import os

bind = "unix:/var/www/dns-geo-check/dns-geo-check.sock"
workers = 2
worker_class = "uvicorn.workers.UvicornWorker"
chdir = "/var/www/dns-geo-check"
accesslog = "app/logs/access.log"
errorlog = "app/logs/app.log"
# The app logs via logging.basicConfig -> stderr; without this, those lines
# (incl. the per-check "dns-geo ..." timing) land in journald instead of app.log.
capture_output = True
access_log_format = '%(t)s %(h)s "%(r)s" %(s)s %(b)s %(L)ss'
loglevel = os.environ.get("LOG_LEVEL", "info")

# nginx proxies to this unix socket (see /etc/nginx/sites-enabled/...),
# so the peer connection has no IP at all — uvicorn's default trusted-proxy
# check (forwarded_allow_ips="127.0.0.1") never matches a unix-socket peer.
# Safe to always-trust here since the socket is only reachable by local nginx.
forwarded_allow_ips = "*"
