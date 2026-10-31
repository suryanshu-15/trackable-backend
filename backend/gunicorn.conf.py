"""Gunicorn settings; every value can be overridden with an environment variable."""
import multiprocessing
import os

bind = os.environ.get('GUNICORN_BIND', '0.0.0.0:8000')
workers = int(os.environ.get('GUNICORN_WORKERS', multiprocessing.cpu_count() * 2 + 1))
threads = int(os.environ.get('GUNICORN_THREADS', 2))
timeout = int(os.environ.get('GUNICORN_TIMEOUT', 60))
graceful_timeout = 30
max_requests = 2000            # recycle workers to bound memory growth
max_requests_jitter = 200
accesslog = '-'
errorlog = '-'
# Trust X-Forwarded-* only from the reverse proxy (set to its address in production)
forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1')
