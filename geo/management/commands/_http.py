"""HTTP helpers for management commands."""

from collections.abc import Callable
import urllib.error
import urllib.request
from urllib.parse import urljoin

from django.core.management.base import CommandError


MAX_VALIDATED_REDIRECTS = 5
ENTITY_HEADERS = {"content-length", "content-type", "transfer-encoding"}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirect following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Return no replacement request so redirects surface as HTTPError.

        Args:
            req: Original request.
            fp: Response file pointer.
            code: HTTP status code.
            msg: HTTP status message.
            headers: Response headers.
            newurl: Redirect target URL.

        Returns:
            None so urllib does not follow the redirect automatically.
        """
        return None


def open_url_with_validated_redirects(
    request: urllib.request.Request,
    timeout: int,
    validate_url: Callable[[str], None],
):
    """Open a URL while validating every redirect target before following it.

    Args:
        request: Initial urllib request.
        timeout: Request timeout in seconds.
        validate_url: Validator called for the initial URL and every redirect.

    Returns:
        Open urllib response object.

    Raises:
        CommandError: If a redirect is unsafe, malformed, or too deep.
        OSError: If urllib cannot complete the request.
    """
    opener = urllib.request.build_opener(NoRedirectHandler())
    current_request = request

    for _redirect_count in range(MAX_VALIDATED_REDIRECTS + 1):
        validate_url(current_request.full_url)
        try:
            response = opener.open(current_request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code < 300 or error.code >= 400:
                raise

            location = error.headers.get("Location")
            if not location:
                raise CommandError(
                    "Redirect response is missing a Location header."
                ) from error

            redirect_url = urljoin(error.geturl(), location)
            validate_url(redirect_url)
            current_request = build_redirect_request(
                current_request,
                redirect_url,
                error.code,
            )
            continue

        validate_url(response.geturl())
        return response

    raise CommandError("Too many redirects.")


def build_redirect_request(
    original_request: urllib.request.Request,
    redirect_url: str,
    status_code: int,
) -> urllib.request.Request:
    """Build a follow-up request for a validated redirect URL.

    Args:
        original_request: Request that received the redirect.
        redirect_url: Validated absolute redirect target.
        status_code: Redirect response status code.

    Returns:
        Request targeting the redirect URL.
    """
    if status_code in {301, 302, 303}:
        return urllib.request.Request(
            redirect_url,
            headers=redirect_get_headers(original_request),
            method="GET",
        )

    return urllib.request.Request(
        redirect_url,
        data=original_request.data,
        headers=dict(original_request.header_items()),
        method=original_request.get_method(),
    )


def redirect_get_headers(
    original_request: urllib.request.Request,
) -> dict[str, str]:
    """Return headers safe to reuse after switching a redirect to GET.

    Args:
        original_request: Request that received the redirect.

    Returns:
        Original headers without request-body-specific headers.
    """
    return {
        name: value
        for name, value in original_request.header_items()
        if name.lower() not in ENTITY_HEADERS
    }
