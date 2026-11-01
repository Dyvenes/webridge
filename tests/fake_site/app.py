"""Минимальный FastAPI-сайт: логин, чат, капча, лимит."""

from __future__ import annotations

from fastapi import Cookie, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()
_COOKIE = "fake_session"

_CHAT_HTML = r"""<!DOCTYPE html>
<html><head><title>Fake Chat</title></head><body>
<div data-testid="sidebar"><a href="/">New chat</a></div>
<div id="messages"></div>
<form id="form" onsubmit="return false">
  <textarea id="input" aria-label="Message" placeholder="Type..."></textarea>
  <button id="send" onclick="sendMessage()">Send</button>
  <button id="stop" style="display:none" onclick="stopGeneration()">Stop</button>
</form>
<script>
let stopRequested = false;
function stopGeneration() {
  stopRequested = true;
  document.getElementById('stop').style.display = 'none';
  document.getElementById('send').disabled = false;
}
async function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value;
  if (!text.trim()) return;
  input.value = '';
  document.getElementById('send').disabled = true;
  document.getElementById('stop').style.display = '';
  stopRequested = false;

  const userDiv = document.createElement('div');
  userDiv.setAttribute('data-message-author', 'user');
  userDiv.textContent = text;
  document.getElementById('messages').appendChild(userDiv);

  const respDiv = document.createElement('div');
  respDiv.setAttribute('data-message-author', 'assistant');
  const copyBtn = document.createElement('button');
  copyBtn.textContent = 'Copy';
  copyBtn.onclick = () => {
    const raw = respDiv.getAttribute('data-raw') || '';
    if (navigator.clipboard) navigator.clipboard.writeText(raw);
  };
  respDiv.appendChild(copyBtn);
  const span = document.createElement('span');
  span.className = 'assistant-content';
  respDiv.appendChild(span);
  document.getElementById('messages').appendChild(respDiv);

  const reply = 'Reply to: ' + text;
  for (let i = 0; i < reply.length; i++) {
    if (stopRequested) break;
    span.textContent += reply[i];
    await new Promise(r => setTimeout(r, 5));
  }
  respDiv.setAttribute('data-raw', span.textContent);
  document.getElementById('stop').style.display = 'none';
  document.getElementById('send').disabled = false;
}
</script></body></html>"""

_LOGIN_HTML = """<!DOCTYPE html>
<html><body><h1>Login</h1>
<form method="post" action="/do_login">
  <input type="password" name="pw" placeholder="password">
  <button type="submit">Login</button>
</form></body></html>"""

_CAPTCHA_HTML = """<!DOCTYPE html>
<html><body><h1>Verify you are human</h1></body></html>"""

_LIMITED_HTML = """<!DOCTYPE html>
<html><body><h1>You have hit the usage cap. Try again later.</h1></body></html>"""


def _html(body: str) -> HTMLResponse:
    return HTMLResponse(body)


@app.get("/", response_class=HTMLResponse)
async def root(fake_session: str | None = Cookie(default=None)):
    if fake_session != "ok":
        return RedirectResponse("/login", status_code=302)
    return _html(_CHAT_HTML)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _html(_LOGIN_HTML)


@app.post("/do_login")
async def do_login(pw: str = Form(default="")):
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(_COOKIE, "ok", httponly=True)
    return response


@app.get("/do_login")
async def do_login_get():
    """Test helper: GET логинит сразу (для тестов сессии)."""
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(_COOKIE, "ok", httponly=True)
    return response


@app.get("/captcha", response_class=HTMLResponse)
async def captcha():
    return _html(_CAPTCHA_HTML)


@app.get("/limited", response_class=HTMLResponse)
async def limited():
    return _html(_LIMITED_HTML)
