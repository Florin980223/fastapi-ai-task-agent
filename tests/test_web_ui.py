"""Tests for the static web UI (app/static/) and the security-header
middleware that protects it.

Covers: the UI route and its static assets are served correctly, no API
key is ever embedded in any served HTML/JS, security headers are present
(with the documented Swagger/ReDoc exemption), and - the actual point of
mounting a catch-all StaticFiles at "/" - every pre-existing route
(including FastAPI's own /docs, /redoc, /openapi.json) is still reachable
and unauthenticated access is still rejected exactly as before.
"""

def test_root_serves_the_ui_shell(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="apiKeyForm"' in response.text


def test_static_css_is_served(client):
    response = client.get("/css/style.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_static_js_modules_are_served(client):
    for path in ("/js/api.js", "/js/app.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]


def test_no_api_key_is_embedded_in_served_ui_assets(unauthenticated_client, test_api_key, other_test_api_key):
    # These are the only real secrets available to this test process - if
    # either ever showed up in a static asset, that would be an actual
    # hardcoded-key regression, not a false positive.
    bodies = [
        unauthenticated_client.get("/").text,
        unauthenticated_client.get("/css/style.css").text,
        unauthenticated_client.get("/js/api.js").text,
        unauthenticated_client.get("/js/app.js").text,
    ]
    for body in bodies:
        assert test_api_key not in body
        assert other_test_api_key not in body


def test_docs_route_still_reachable(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_redoc_route_still_reachable(client):
    response = client.get("/redoc")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_openapi_json_still_reachable(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "openapi" in response.json()


def test_health_route_still_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_tasks_still_requires_authentication(unauthenticated_client):
    response = unauthenticated_client.get("/tasks")
    assert response.status_code == 401


def test_unknown_api_style_path_returns_404_not_the_ui_shell(client):
    response = client.get("/some-unknown-api-style-path")

    assert response.status_code == 404
    assert 'id="apiKeyForm"' not in response.text


def test_security_headers_present_on_api_and_ui_responses(client):
    for path in ("/health", "/"):
        response = client.get(path)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Frame-Options"] == "DENY"
        csp = response.headers["Content-Security-Policy"]
        assert "'unsafe-inline'" not in csp
        assert "'unsafe-eval'" not in csp
        assert "http://" not in csp and "https://" not in csp
        assert "default-src 'self'" in csp


def test_docs_route_is_exempt_from_content_security_policy(client):
    response = client.get("/docs")

    assert "Content-Security-Policy" not in response.headers
    # The other security headers still apply even to the exempted paths.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
