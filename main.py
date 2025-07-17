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
from typing import List

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(levelname)s:     %(message)s')
logger = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Server Name Detection Logic ---
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
    
    ip = await resolve_ip_async(hostname)
    if is_akamai_ip(ip):
        return "Akamai"
        
    return "Unknown"

# --- Redirect Tracing Logic ---
async def trace_redirect_chain(client: httpx.AsyncClient, initial_url: str):
    redirect_chain = []
    current_url = initial_url
    max_hops = 20
    try:
        for _ in range(max_hops):
            try:
                response = await client.get(current_url, follow_redirects=False, timeout=20.0)
                server_name = await get_server_name_advanced(dict(response.headers), str(response.url))
                hop_data = {"url": str(response.url), "status": response.status_code, "server": server_name}
                redirect_chain.append(hop_data)

                if not response.is_redirect:
                    return {"originalURL": initial_url, "finalURL": str(response.url), "finalStatus": response.status_code, "redirectChain": redirect_chain, "error": None}
                
                next_location = response.headers.get('location')
                if not next_location:
                    return {"originalURL": initial_url, "finalURL": str(response.url), "finalStatus": response.status_code, "redirectChain": redirect_chain, "error": "Redirect with no Location header."}
                
                current_url = urlparse(next_location, scheme=response.url.scheme, netloc=response.url.netloc).geturl()

            except httpx.RequestError as e:
                return {"originalURL": initial_url, "error": f"Request Failed: {type(e).__name__}"}
        
        return {"originalURL": initial_url, "error": f"Exceeded maximum redirects ({max_hops} hops)."}
    except Exception as e:
        logger.error(f"Unexpected error tracing {initial_url}: {e}")
        return {"originalURL": initial_url, "error": "An unexpected server error occurred."}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection open.")
    try:
        data = await websocket.receive_text()
        urls_to_process = [url.strip() for url in data.splitlines() if url.strip()]
        
        CONCURRENCY_LIMIT = 50
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def bound_check(url, client):
            async with semaphore:
                return await trace_redirect_chain(client, url)

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        async with httpx.AsyncClient(headers=headers, verify=False) as client: # Added verify=False for flexibility
            tasks = []
            for url in urls_to_process:
                if not url.startswith(("http://", "https://")):
                    url = f"https://{url}"
                tasks.append(asyncio.create_task(bound_check(url, client)))

            for future in asyncio.as_completed(tasks):
                result = await future
                await websocket.send_json(result)
        
        await websocket.send_json({"status": "done"})

    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    except Exception as e:
        logger.error(f"An error occurred in WebSocket: {e}")
    finally:
        logger.info("Processing complete. Closing connection.")

@app.get("/")
async def read_index():
    return FileResponse('index.html')

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
