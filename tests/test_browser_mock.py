"""Exercises the Playwright code path (gallery pop-up, lazy loading, room pop-ups, cookie banner)
against a local MOCK page. This proves the mechanics work; it does NOT prove Booking.com's real markup matches."""
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bsync.scraper import BookingBrowser

try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

PAGE = """<!doctype html><html lang="de"><head><title>Mock Hotel</title>
<script type="application/ld+json">{"@type":"Hotel","name":"Mock Hotel"}</script>
<script type="application/json" id="state">{"rooms":[{"id":9001,"name":"Nur im JSON Suite"}],
 "hotelPhotos":[{"large_url":"https://cf.bstatic.com/xdata/images/hotel/max1024x768/5005.jpg?k=zz&o=","associated_rooms":[9001]}]}</script>
<style>#dlg{position:fixed;inset:5% 10%;background:#fff;border:1px solid #000;overflow:auto;height:300px}</style></head>
<body>
<div id="onetrust-banner"><button id="onetrust-accept-btn-handler">Akzeptieren</button></div>
<div class="bh-photo-grid"><a class="bh-photo-grid-item" href="#"><img src="/xdata/images/hotel/max300/1001.jpg" alt="Lobby"></a></div>
<table id="hprt-table"><tr><td><a class="hprt-roomtype-link" href="#">Standardzimmer<br>(12 m²)</a></td></tr>
<tr><td><a class="hprt-roomtype-link" href="#">Familienzimmer</a></td></tr>
<tr><td><a class="hprt-roomtype-link" href="#">Standardzimmer<br>(12 m²)</a></td></tr></table>
<h3>Ähnliche Unterkünfte</h3><img src="/xdata/images/hotel/square200/7777.jpg">
<script>
document.getElementById('onetrust-accept-btn-handler').onclick = () => document.getElementById('onetrust-banner').remove();
function closeBtn(d){ const b=document.createElement('button'); b.setAttribute('aria-label','Close'); b.textContent='x'; b.onclick=()=>d.remove(); return b; }
function open(ids, lazy){
  const d=document.createElement('div'); d.id='dlg'; d.setAttribute('role','dialog'); d.appendChild(closeBtn(d));
  const box=document.createElement('div'); d.appendChild(box); document.body.appendChild(d);
  const add=(a)=>a.forEach(i=>{const im=new Image(); im.src='/xdata/images/hotel/max300/'+i+'.jpg?k=x&o='; im.alt='Zimmer mit Bett '+i; im.height=120; im.style.display='block'; box.appendChild(im);});
  if(!lazy){ add(ids); return; }
  let next=0; const step=()=>{ add(ids.slice(next,next+3)); next+=3; };
  step(); d.onscroll=()=>{ if(next<ids.length && d.scrollTop+d.clientHeight>d.scrollHeight-60) step(); };
}
document.querySelector('.bh-photo-grid-item').onclick=(e)=>{e.preventDefault(); open([1001,1002,1003,1004,1005,1006,1007,1008],true);};
const links=document.querySelectorAll('.hprt-roomtype-link');
links[0].onclick=(e)=>{e.preventDefault(); open([2001,2002,1003],false);};
links[1].onclick=(e)=>{e.preventDefault(); open([3001],false);};
links[2].onclick=links[0].onclick;
</script></body></html>"""

PIXEL = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/hotel/"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            body, ctype = PIXEL, "image/png"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class BrowserMock(unittest.TestCase):
    def test_scrape_mock_hotel(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/hotel/de/mock.de.html"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    br = BookingBrowser(Path(tmp) / "profile", Path(tmp) / "debug", headed=False,
                                        channel="chromium", pace=(0.05, 0.1), challenge_wait=30)
                    br.__enter__()
                except Exception as e:
                    self.skipTest(f"no browser available: {str(e)[:100]}")
                try:
                    res = br.scrape(1, url, "Mock Hotel", (None, None))
                finally:
                    br.__exit__(None, None, None)
        finally:
            srv.shutdown()

        self.assertEqual(res.status, "OK", res.notes)
        got = sorted((i.category or "-", i.pid) for i in res.images)
        self.assertEqual(got, [
            ("-", "1001"), ("-", "1002"), ("-", "1004"), ("-", "1005"), ("-", "1006"), ("-", "1007"), ("-", "1008"),
            ("Familienzimmer", "3001"),
            ("Nur im JSON Suite", "5005"),
            ("Standardzimmer (12 m²)", "1003"), ("Standardzimmer (12 m²)", "2001"), ("Standardzimmer (12 m²)", "2002"),
        ])
        self.assertNotIn("7777", {i.pid for i in res.images}, "'similar properties' decoy must be ignored")
        self.assertEqual(res.diagnostics["gallery_photos"], 8, "lazy-loaded gallery fully scrolled")
        self.assertEqual(res.diagnostics["room_links"], 3)
        self.assertEqual(res.diagnostics["room_popups_opened"], 2, "duplicate room link opened once")
        json_photo = next(i for i in res.images if i.pid == "5005")
        self.assertIn("max1024x768/5005.jpg?k=zz", json_photo.url)
        lobby = next(i for i in res.images if i.pid == "1001")
        self.assertEqual(lobby.caption, "Zimmer mit Bett 1001")
        self.assertTrue((Path(tmp) / "debug").name == "debug")


if __name__ == "__main__":
    unittest.main()
