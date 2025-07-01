# main.py
import asyncio
import httpx
import uvicorn
import logging
import socket
import ipaddress
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from urllib.parse import urlparse

# --- Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Server Name Detection Logic (omitted for brevity, no changes here) ---
AKAMAI_IP_RANGES = ["23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"]
ip_cache = {}

async def resolve_ip_async(hostname: str):
    if not hostname: return None
    if hostname in ip_cache: return ip_cache[hostname]
    try:
        ip = await asyncio.to_thread(socket.gethostbyname, hostname)
        ip_cache[hostname] = ip
        return ip
    except (socket.gaierror, TypeError): return None

def is_akamai_ip(ip: str) -> bool:
    if not ip: return False
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in AKAMAI_IP_RANGES:
            if addr in ipaddress.ip_network(cidr): return True
    except ValueError: pass
    return False

async def get_server_name_advanced(headers: dict, url: str) -> str:
    headers = {k.lower(): v for k, v in headers.items()}
    hostname = urlparse(url).hostname
    server_value = headers.get("server", "").lower()
    if server_value:
        if "akamai" in server_value or "ghost" in server_value: return "Akamai"
        if "apache" in server_value: return "Apache (AEM)"
        return server_value.capitalize()
    server_timing = headers.get("server-timing", "")
    has_akamai_cache = "cdn-cache; desc=HIT" in server_timing or "cdn-cache; desc=MISS" in server_timing
    has_akamai_request_id = "x-akamai-request-id" in headers
    has_dispatcher = "x-dispatcher" in headers or "x-aem-instance" in headers
    has_aem_paths = any("/etc.clientlibs" in v for h, v in headers.items() if h in ["link", "baqend-tags"])
    ip = await resolve_ip_async(hostname)
    is_akamai = is_akamai_ip(ip)
    if has_akamai_cache or has_akamai_request_id or (server_timing and is_akamai):
        if has_aem_paths or has_dispatcher: return "Apache (AEM)"
        return "Akamai"
    if has_dispatcher or has_aem_paths: return "Apache (AEM)"
    if is_akamai: return "Akamai"
    return "Unknown"

# --- CORRECTED FUNCTION TO TRACE REDIRECTS ---
async def check_url_status(client: httpx.AsyncClient, url: str):
    redirect_chain = []
    current_url = url
    final_status = ""
    final_comment = ""
    final_server_name = "N/A"
    MAX_REDIRECTS = 15

    try:
        for i in range(MAX_REDIRECTS):
            response = await client.get(current_url, follow_redirects=False, timeout=20.0)
            server_name = await get_server_name_advanced(response.headers, str(response.url))
            
            # The first hop determines the overall server name
            if i == 0:
                final_server_name = server_name

            if response.is_redirect:
                hop_info = {"status": response.status_code, "url": response.headers.get('location', 'N/A')}
                redirect_chain.append(hop_info)
                current_url = hop_info["url"]
                if not current_url:
                    final_status = response.status_code
                    final_comment = "Redirect missing location"
                    break
            else:
                # Final destination reached
                response.raise_for_status()
                final_status = response.status_code
                final_comment = "OK"
                # If there was a redirect, the final hop is also part of the chain
                if redirect_chain:
                    final_hop_info = {"status": response.status_code, "url": str(response.url)}
                    redirect_chain.append(final_hop_info)
                break
        else:
             # Loop finished, meaning too many redirects
            final_status = "Error"
            final_comment = "Too many redirects"

    except httpx.HTTPStatusError as e:
        final_status = e.response.status_code
        final_comment = "Not Found" if e.response.status_code == 404 else "Client/Server Error"
    except httpx.RequestError as e:
        final_status = "Error"
        final_comment = f"Request failed: {type(e).__name__}"
    except Exception as e:
        final_status = "Error"
        final_comment = f"An unexpected error occurred: {e}"

    # For display purposes, the primary status is the first one encountered
    display_status = redirect_chain[0]['status'] if redirect_chain else final_status
    display_comment = "Redirect Chain" if redirect_chain else final_comment
    
    return {
        "url": url, 
        "status": display_status, 
        "comment": display_comment, 
        "serverName": final_server_name,
        "redirectChain": redirect_chain
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Connection open")
    try:
        data = await websocket.receive_text()
        urls = [url.strip() for url in data.splitlines() if url.strip()]
        
        CONCURRENCY_LIMIT = 100
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def bound_check(url, client):
            async with semaphore:
                return await check_url_status(client, url)

        async with httpx.AsyncClient() as client:
            tasks = []
            for url in urls:
                if not url.startswith(("http://", "https://")): url = f"https://{url}"
                try:
                    parsed_url = urlparse(url)
                    if not (parsed_url.scheme and parsed_url.netloc): raise ValueError
                except ValueError:
                    await websocket.send_json({"url": url, "status": "Invalid", "comment": "Improper URL structure", "serverName": "N/A", "redirectChain": []})
                    continue
                tasks.append(asyncio.create_task(bound_check(url, client)))

            for future in asyncio.as_completed(tasks):
                result = await future
                await websocket.send_json(result)
        
        logger.info("All tasks complete. Sending 'done' signal to client.")
        await websocket.send_json({"status": "done"})
    except WebSocketDisconnect:
        logger.info("Client disconnected prematurely.")
    except Exception as e:
        logger.error(f"An error occurred in WebSocket: {e}")
    finally:
        logger.info("Processing complete. Closing connection.")

@app.get("/")
async def read_index(): return FileResponse('index.html')

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
