"""Refuse an in-container verifier when RAG_API selects another deployment."""
import sys
from urllib.parse import urlsplit

def matches_local_proxy(api, bindings):
    try:
        url=urlsplit(api)
        if url.scheme!='http' or url.hostname not in ('localhost','127.0.0.1','::1') or url.username or url.password or url.path.rstrip('/')!='/api' or url.query or url.fragment:
            return False
        port=url.port or 80
        for binding in bindings.splitlines():
            host, published=binding.rsplit(':',1)
            host=host.strip('[]')
            if int(published)!=port:continue
            if host in ('0.0.0.0','::') or host==url.hostname or (host=='127.0.0.1' and url.hostname=='localhost'):
                return True
        return False
    except (ValueError,TypeError):return False

if __name__=='__main__':
    if len(sys.argv)!=3 or not matches_local_proxy(sys.argv[1],sys.argv[2]):
        print('This in-container check requires RAG_API to select this Compose proxy on a published loopback port. Remote or mismatched targets are unsupported; no backend check ran.',file=sys.stderr)
        sys.exit(2)
