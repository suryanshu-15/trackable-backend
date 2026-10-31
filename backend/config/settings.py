"""
Django settings for Trackable — Warehouse Material Traceability System.
"""
import sys
from pathlib import Path
from datetime import timedelta
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='dev-insecure-key-change-me')
DEBUG = config('DEBUG', default=False, cast=bool)
if not DEBUG and (SECRET_KEY.startswith('dev-insecure') or SECRET_KEY.startswith('your-secret-key') or len(SECRET_KEY) < 32):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('Set a strong SECRET_KEY (32+ random characters) when DEBUG=False.')
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

# Application definition
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    'django_filters',
    'corsheaders',
]

LOCAL_APPS = [
    'apps.core',
    'apps.users',
    'apps.master',
    'apps.receiving',
    'apps.inventory',
    'apps.laboratory',
    'apps.notifications',
    'apps.reports',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'apps.core.middleware.RequestIDMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='trackable'),
        'USER': config('DB_USER', default='trackable'),
        'PASSWORD': config('DB_PASSWORD', default='trackable'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'OPTIONS': {
            'connect_timeout': 10,
        },
        'CONN_MAX_AGE': 60,
    }
}

# Auth
AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
# Timestamps are stored in UTC (USE_TZ). TIME_ZONE is the business timezone, so
# "today", __date lookups and generated reference dates follow the warehouse's day.
BUSINESS_TIMEZONE = config('BUSINESS_TIMEZONE', default='UTC')
TIME_ZONE = BUSINESS_TIMEZONE
USE_I18N = True
USE_TZ = True

# Static & Media
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

MEDIA_URL = config('MEDIA_URL', default='/media/')
MEDIA_ROOT = config('MEDIA_ROOT', default=str(BASE_DIR / 'mediafiles'))

# QR labels encode <FRONTEND_BASE_URL>/scan/<opaque token>; the token resolves server-side
FRONTEND_BASE_URL = config('FRONTEND_BASE_URL', default='http://localhost:3000').rstrip('/')

MAX_UPLOAD_SIZE_MB = 20
ALLOWED_UPLOAD_EXTENSIONS = [
    '.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.bmp',
    '.xlsx', '.xls', '.csv', '.docx', '.doc', '.txt',
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'apps.core.pagination.StandardResultsSetPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'apps.core.exceptions.custom_exception_handler',
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'DEFAULT_THROTTLE_RATES': {
        # Brute-force protection on the login endpoint (per client IP)
        'login': config('LOGIN_THROTTLE_RATE', default='10/min'),
    },
    'DEFAULT_PARSER_CLASSES': (
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
    ),
}

# Cache — used for login throttling. Use Redis when running several workers.
REDIS_URL = config('REDIS_URL', default='')
CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': REDIS_URL}
    if REDIS_URL else {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}

# JWT
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(
        minutes=config('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', default=60, cast=int)
    ),
    'REFRESH_TOKEN_LIFETIME': timedelta(
        days=config('JWT_REFRESH_TOKEN_LIFETIME_DAYS', default=7, cast=int)
    ),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# CORS
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000',
    cast=Csv(),
)
CORS_ALLOW_CREDENTIALS = True

# OpenAPI
SPECTACULAR_SETTINGS = {
    'TITLE': 'Trackable API',
    'DESCRIPTION': 'Warehouse Material Traceability System API',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    # Stable enum names for the generated TypeScript client
    'ENUM_NAME_OVERRIDES': {
        'ReceiptStatusEnum': 'apps.receiving.models.ReceiptStatus',
        'PackageStatusEnum': 'apps.inventory.models.PackageStatus',
        'QCStatusEnum': 'apps.inventory.models.QCStatus',
        'SampleStatusEnum': 'apps.laboratory.models.SampleStatus',
        'IssueStatusEnum': 'apps.inventory.models.IssueStatus',
        'RequestStatusEnum': 'apps.inventory.models.RequestStatus',
        'DisposalTypeEnum': 'apps.inventory.models.DisposalType',
        'VerificationStatusEnum': 'apps.receiving.models.VerificationStatus',
        'DispositionDecisionEnum': 'apps.laboratory.models.DispositionDecision',
        'RemainderDecisionEnum': [('REJECTED', 'Rejected'), ('ON_HOLD', 'On Hold')],
        'LabDocumentTypeEnum': 'apps.laboratory.models.LabDocumentType',
        'ReceiptDocumentTypeEnum': 'apps.receiving.models.DocumentType',
        'StockAdjustmentStatusEnum': [('PENDING', 'Pending Approval'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected')],
    },
    'TAGS': [
        {'name': 'auth', 'description': 'Authentication'},
        {'name': 'users', 'description': 'User management'},
        {'name': 'master', 'description': 'Master data'},
        {'name': 'receiving', 'description': 'Gate & Receipt'},
        {'name': 'inventory', 'description': 'Inventory & Packages'},
        {'name': 'laboratory', 'description': 'Lab & QC'},
        {'name': 'issue', 'description': 'Material requests, issue & returns'},
        {'name': 'reports', 'description': 'Reports & Exports'},
        {'name': 'search', 'description': 'Global search'},
        {'name': 'audit', 'description': 'Audit trail'},
        {'name': 'notifications', 'description': 'In-app notifications'},
    ],
}

# HTTPS / cookie hardening outside local development
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} [{request_id}] {module} {message}',
            'style': '{',
        },
        'json': {
            '()': 'apps.core.logging.JsonFormatter',
        },
    },
    'filters': {
        'request_id': {'()': 'apps.core.middleware.RequestIDLogFilter'},
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': config('LOG_FORMAT', default='verbose'),
            'filters': ['request_id'],
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': 'DEBUG' if DEBUG else 'INFO', 'propagate': False},
    },
}

if 'test' in sys.argv:
    # Hashing cost is irrelevant in tests and dominates their runtime
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    # Login throttling is tested explicitly; keep it out of the way elsewhere
    REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {'login': '1000/min'}
    # 4xx responses are expected in permission/validation tests; keep output readable
    LOGGING['loggers']['django.request'] = {'handlers': ['console'], 'level': 'ERROR', 'propagate': False}
