import pytest

SECTIONS = ["/configure/", "/build/", "/repo/", "/deploy/", "/settings/"]


def test_root_redirects_to_configure(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configure/")


@pytest.mark.parametrize("path", SECTIONS)
def test_section_renders_with_sidebar(client, path):
    response = client.get(path)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for label in ("Configure", "Build", "Repo", "Deploy", "Settings"):
        assert f">{label}</a>" in html
    assert 'aria-current="page"' in html


def test_settings_is_last_in_nav(client):
    html = client.get("/").get_data(as_text=True)
    html = client.get("/configure/").get_data(as_text=True)
    assert html.index(">Deploy</a>") < html.index(">Settings</a>")


def test_html_404_and_json_api_404(client):
    assert "<html" in client.get("/nope").get_data(as_text=True)
    assert client.get("/configure/api/nope").get_json() == {"error": "Not Found"}
