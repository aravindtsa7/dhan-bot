"""Run unittest with external sockets blocked; loopback supports Windows asyncio."""
import ipaddress
import socket
import sys
import unittest


def main(names=None):
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    blocked=[]
    def check(address):
        if isinstance(address,tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback: return
            except ValueError: pass
        blocked.append(True)
        raise AssertionError('External network connections are forbidden during offline tests')
    def guarded(sock,address):
        check(address);return connect(sock,address)
    def guarded_ex(sock,address):
        check(address);return connect_ex(sock,address)
    socket.socket.connect=guarded;socket.socket.connect_ex=guarded_ex
    try:
        suite=unittest.defaultTestLoader.loadTestsFromNames(names) if names else unittest.defaultTestLoader.discover('.',pattern='test_*.py')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        socket.socket.connect=connect;socket.socket.connect_ex=connect_ex
    print('External network attempts:',len(blocked))
    return int(not result.wasSuccessful() or bool(blocked))


if __name__=='__main__': raise SystemExit(main(sys.argv[1:]))
