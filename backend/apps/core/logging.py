"""
JSON structured logger formatter.
"""
import json
import logging
import traceback
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            'timestamp': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'funcName': record.funcName,
            'lineno': record.lineno,
            'request_id': getattr(record, 'request_id', '-'),
        }
        if record.exc_info:
            log_entry['exception'] = traceback.format_exception(*record.exc_info)
        return json.dumps(log_entry)
