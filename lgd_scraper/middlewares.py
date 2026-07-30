from __future__ import annotations

import ast
import base64
import binascii
import logging
import re

from scrapy import Request
from scrapy.http import Response


LOGGER = logging.getLogger(__name__)

_OUTER_PAYLOAD_RE = re.compile(
    r"\bS=(?P<quote>['\"])(?P<payload>[A-Za-z0-9+/=]+)(?P=quote)"
)
_ASSIGNMENT_RE = re.compile(
    r"(?P<variable>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"(?P<value>.*?)\s*;\s*document\.cookie\s*=\s*"
    r"(?P<cookie>.*?)\s*;\s*location\.reload",
    re.DOTALL,
)
_CONCAT_TERM_RE = re.compile(
    r"""
    \s*(?:\+\s*)?
    (?:
        String\.fromCharCode\((?P<code>\d{1,3})\)
        |
        (?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    )
    """,
    re.VERBOSE,
)
_COOKIE_NAME_RE = re.compile(r"sucuri_cloudproxy_uuid_[A-Za-z0-9_-]+")
_COOKIE_VALUE_RE = re.compile(r"[A-Fa-f0-9]{16,128}")


def _decode_concat_expression(expression: str) -> str | None:
    """Decode a restricted sequence of literals and String.fromCharCode calls."""

    expression = expression.strip().rstrip("+").strip()
    position = 0
    decoded: list[str] = []
    while position < len(expression):
        match = _CONCAT_TERM_RE.match(expression, position)
        if not match:
            return None
        if match.group("code") is not None:
            code = int(match.group("code"))
            if code > 255:
                return None
            decoded.append(chr(code))
        else:
            try:
                literal = ast.literal_eval(match.group("string"))
            except (SyntaxError, ValueError):
                return None
            if not isinstance(literal, str):
                return None
            decoded.append(literal)
        position = match.end()
    return "".join(decoded)


def parse_sucuri_cookie(body: str) -> tuple[str, str] | None:
    """Extract the browser cookie from Sucuri's base64-wrapped JS challenge."""

    if "sucuri_cloudproxy_js" not in body:
        return None
    outer_match = _OUTER_PAYLOAD_RE.search(body)
    if not outer_match:
        return None
    try:
        decoded_script = base64.b64decode(
            outer_match.group("payload"), validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None

    assignment = _ASSIGNMENT_RE.search(decoded_script)
    if not assignment:
        return None
    value = _decode_concat_expression(assignment.group("value"))
    if not value or not _COOKIE_VALUE_RE.fullmatch(value):
        return None

    variable = assignment.group("variable")
    cookie_expression = assignment.group("cookie")
    variable_reference = re.search(
        rf"(?P<name>.*?)\+\s*{re.escape(variable)}(?:\s*\+|$)",
        cookie_expression,
        re.DOTALL,
    )
    if not variable_reference:
        return None
    name_with_separator = _decode_concat_expression(
        variable_reference.group("name")
    )
    if not name_with_separator or not name_with_separator.endswith("="):
        return None
    name = name_with_separator[:-1]
    if not _COOKIE_NAME_RE.fullmatch(name):
        return None
    return name, value


class SucuriCookieChallengeMiddleware:
    """Complete Sucuri's normal JavaScript cookie handshake without a browser."""

    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings.getint("SUCURI_CHALLENGE_MAX_RETRIES", 2))

    def process_response(self, request: Request, response: Response) -> Response | Request:
        if response.status != 307:
            return response
        challenge = parse_sucuri_cookie(response.text)
        if not challenge:
            return response

        retries = int(request.meta.get("sucuri_challenge_retries", 0))
        if retries >= self.max_retries:
            LOGGER.error(
                "Sucuri cookie challenge persisted after %d retries for %s",
                retries,
                request.url,
            )
            return response

        cookie_name, cookie_value = challenge
        cookies = dict(request.cookies) if isinstance(request.cookies, dict) else {}
        cookies[cookie_name] = cookie_value
        meta = dict(request.meta)
        meta["sucuri_challenge_retries"] = retries + 1
        meta["dont_cache"] = True
        LOGGER.info(
            "Completing Sucuri browser cookie challenge for %s", request.url
        )
        return request.replace(
            cookies=cookies,
            meta=meta,
            dont_filter=True,
            priority=request.priority + 10,
        )
