#!/usr/bin/env python3
"""Certificate-authenticated remote USB controller with reconnect and heartbeats."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import select
import signal
import socket
import ssl
import struct
import sys
import time

ROOT=Path(sys.argv[1] if len(sys.argv)>1 else '/boot/config/plugins/wiimote-relay')
RUN=Path('/var/run/wiimote-relay');RUN.mkdir(mode=0o700,parents=True,exist_ok=True)
lock=open(RUN/'client.lock','w')
try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:raise SystemExit('Relay client is already running')
log=logging.getLogger('wiimote-relay');log.setLevel(logging.INFO)
h=RotatingFileHandler('/var/log/wiimote-relay.log',maxBytes=1_000_000,backupCount=2)
h.setFormatter(logging.Formatter('%(asctime)s %(message)s'));log.addHandler(h)
running=True

def stop(*args):
    global running
    running=False
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)

def state(**data):
    data['updated_at']=time.time()
    p=RUN/'status.tmp'
    p.write_text(json.dumps(data));p.chmod(0o600);os.replace(p,RUN/'status.json')

def exact(sock,n):
    result=bytearray()
    while len(result)<n:
        part=sock.recv(n-len(result))
        if not part:raise EOFError('TLS peer closed during startup')
        result.extend(part)
    return bytes(result)

def session(cfg,ctx):
    raw=socket.create_connection((cfg['host'],cfg.get('port',18765)),timeout=10)
    raw.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    raw.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
    for option,value in [('TCP_KEEPIDLE',20),('TCP_KEEPINTVL',5),('TCP_KEEPCNT',3),('TCP_USER_TIMEOUT',30000)]:
        if hasattr(socket,option):raw.setsockopt(socket.IPPROTO_TCP,getattr(socket,option),value)
    conn=None;fd=None
    try:
        conn=ctx.wrap_socket(raw,server_hostname='wiimote-onn')
        conn.settimeout(30)
        hello=exact(conn,10)
        if hello[:4]!=b'WMR1':raise RuntimeError('Unexpected authenticated relay protocol')
        address=':'.join(f'{x:02X}' for x in reversed(hello[4:]))
        if address!=cfg['controller_address']:raise RuntimeError('Relay controller identity mismatch')
        fd=os.open('/dev/vhci',os.O_RDWR|os.O_NONBLOCK|os.O_CLOEXEC)
        os.write(fd,b'\xff\x00')
        if not select.select([fd],[],[],5)[0]:raise TimeoutError('VHCI startup timed out')
        response=os.read(fd,4096)
        if len(response)!=4 or response[:2]!=b'\xff\x00':raise RuntimeError('Unexpected VHCI startup response')
        adapter='hci'+str(int.from_bytes(response[2:4],'little'))
        if adapter!=cfg.get('adapter','hci1'):raise RuntimeError('Unexpected virtual adapter '+adapter)
        log.info('Connected to ONN %s via %s; %s controller %s',cfg['host'],conn.version(),adapter,address)
        state(state='connected',host=cfg['host'],adapter=adapter,controller_address=address,tls=conn.version())
        conn.setblocking(False)
        incoming=bytearray();outgoing=bytearray();send_chunk=None;last_rx=time.monotonic();heartbeat=last_rx
        while running:
            now=time.monotonic()
            if now-last_rx>35:raise TimeoutError('Relay heartbeat timed out')
            if now-heartbeat>=10:outgoing.extend(b'\0\0');heartbeat=now
            reads,writes,_=select.select([conn,fd],[conn] if outgoing else [],[],1)
            if fd in reads:
                packet=os.read(fd,4096)
                if not packet:raise EOFError('VHCI device closed')
                if len(outgoing)+len(packet)>1_048_576:raise RuntimeError('Relay transmit queue exceeded limit')
                outgoing.extend(struct.pack('!H',len(packet))+packet)
            if conn in writes:
                try:
                    if send_chunk is None:send_chunk=bytes(outgoing[:16384])
                    n=conn.send(send_chunk)
                    if not n:raise EOFError('TLS write closed')
                    del outgoing[:n]
                    send_chunk=None
                except (ssl.SSLWantReadError,ssl.SSLWantWriteError):pass
            if conn in reads or conn.pending():
                try:chunk=conn.recv(16384)
                except (ssl.SSLWantReadError,ssl.SSLWantWriteError):continue
                if not chunk:raise EOFError('ONN disconnected')
                incoming.extend(chunk);last_rx=time.monotonic()
                while len(incoming)>=2:
                    n=int.from_bytes(incoming[:2],'big')
                    if n>4096:raise RuntimeError('Invalid relay frame')
                    if len(incoming)<n+2:break
                    packet=bytes(incoming[2:n+2]);del incoming[:n+2]
                    if n:
                        if packet[0] not in (2,4):raise RuntimeError('Invalid controller packet type')
                        if os.write(fd,packet)!=len(packet):raise RuntimeError('Short VHCI write')
    finally:
        if fd is not None:os.close(fd)
        if conn:conn.close()
        else:raw.close()

log.info('Unraid persistent relay client started')
try:
    while running:
        try:
            cfg=json.loads((ROOT/'config.json').read_text())
            ctx=ssl.create_default_context(cafile=str(ROOT/'tls/ca.crt'))
            ctx.load_cert_chain(str(ROOT/'tls/client.crt'),str(ROOT/'tls/client.key'))
            ctx.minimum_version=ssl.TLSVersion.TLSv1_2
            session(cfg,ctx)
        except Exception as exc:
            log.warning('Relay disconnected: %s',exc);state(state='reconnecting',error=str(exc))
        for _ in range(3):
            if not running:break
            time.sleep(1)
finally:
    state(state='stopped');log.info('Unraid persistent relay client stopped')
