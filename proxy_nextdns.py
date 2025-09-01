import asyncio
import logging
import aiohttp
import time
import dns.message
import dns.rdatatype
import ipaddress

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger('proxy')

# ---------------- Configurações -----------------
DNS_CACHE = {}          # cache simples {hostname: (ip, timestamp)}
CACHE_TTL = 300         # tempo de vida do cache em segundos

# ---------------- DNS via NextDNS DoH -----------------
async def resolve_host(hostname, nextdns_id):
    now = time.time()
    if hostname in DNS_CACHE:
        ip, ts = DNS_CACHE[hostname]
        if now - ts < CACHE_TTL:
            return ip

    query = dns.message.make_query(hostname, dns.rdatatype.A)
    query_wire = query.to_wire()
    url = f"https://dns.nextdns.io/{nextdns_id}/dns-query"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=query_wire,
                headers={"Content-Type": "application/dns-message"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    msg = dns.message.from_wire(data)
                    for answer in msg.answer:
                        for item in answer.items:
                            if item.rdtype == dns.rdatatype.A:
                                ip = item.address
                                DNS_CACHE[hostname] = (ip, now)
                                return ip
    except Exception as e:
        logger.warning(f"Erro ao resolver {hostname} via NextDNS: {e}")
    return None

# ---------------- Forwarding -----------------
async def forward(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except:
        pass
    finally:
        writer.close()
        await writer.wait_closed()

# ---------------- Manipulação do Cliente -----------------
async def handle_client(reader, writer, nextdns_id):
    try:
        data = await reader.read(4096)
        if not data:
            writer.close()
            await writer.wait_closed()
            return

        request_line = data.decode(errors='ignore').split('\n')[0]
        if not request_line.strip():
            writer.close()
            await writer.wait_closed()
            return

        parts = request_line.strip().split()
        if len(parts) < 3:
            writer.close()
            await writer.wait_closed()
            return

        method, path, version = parts

        if method == 'GET' and path == '/':
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html\r\n"
                "Connection: close\r\n\r\n"
                "<html><body><h1>PROXY DNS</h1></body></html>"
            )
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        if method == 'CONNECT':
            host, port = path.split(':')
            port = int(port)
        else:
            host = None
            for line in data.decode(errors='ignore').split('\r\n'):
                if line.lower().startswith("host:"):
                    host = line.split(":")[1].strip()
                    break
            port = 80

        if not host:
            writer.close()
            await writer.wait_closed()
            return

        # Checar se host já é um IP
        try:
            ipaddress.ip_address(host)
            ip = host
        except ValueError:
            # Não é IP, resolve via NextDNS
            ip = await resolve_host(host, nextdns_id)
            logger.info(f'IP DO {host} RESOLVIDO É: {ip}')
            if not ip:
                logger.warning(f"Não foi possível resolver {host}")
                writer.close()
                await writer.wait_closed()
                return

        # Conectar ao host remoto
        try:
            remote_reader, remote_writer = await asyncio.open_connection(ip, port)
        except Exception as e:
            writer.close()
            await writer.wait_closed()
            return

        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            remote_writer.write(data)
            await remote_writer.drain()

        await asyncio.gather(
            forward(reader, remote_writer),
            forward(remote_reader, writer)
        )

    except Exception as e:
        writer.close()
        await writer.wait_closed()

# ---------------- Servidor -----------------
async def main(port, nextdns_id):
    logger.info(f"Proxy rodando na porta {port} com NextDNS ID {nextdns_id}...")
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, nextdns_id),
        '0.0.0.0',
        port
    )
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Uso: python proxy_nextdns.py <PORTA> <NEXTDNS_ID>")
        sys.exit(1)

    port = int(sys.argv[1])
    nextdns_id = sys.argv[2]
    asyncio.run(main(port, nextdns_id))
