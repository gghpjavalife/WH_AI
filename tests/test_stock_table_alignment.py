import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import tempfile
import threading
import unittest

from features.equities.scan_table import render_scan_table
from test_context_table_filters import row


_BROWSER = next((path for path in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
) if os.path.isfile(path)), None)


@unittest.skipUnless(_BROWSER, "Chrome or Edge is required for layout verification")
class StockTableAlignmentTests(unittest.TestCase):
    def test_header_filter_and_values_align_in_browser(self):
        config = {
            "signals": ["BUY"], "groups": [], "info": {}, "checkInfo": {},
            "rawCols": ["Extra metric"], "rawTypes": ["number"],
            "columns": [["ticker", "Ticker", 0], ["quantity", "Qty", 1, "number"],
                        ["price", "LTP", 1, "currency"], ["signal", "Signal", 0]],
        }
        html = render_scan_table([
            row("FORCEMOT", [123], quantity=4, price=18271.25),
        ], config)
        measure = r"""
        <script>
        const measure=()=>{
          const h=document.querySelectorAll('#head th');
          const f=document.querySelectorAll('#filt td');
          const d=document.querySelectorAll('#body tr.row td');
          const check=(nodes,i)=>{
           const element=nodes[i], r=element.getBoundingClientRect(),s=getComputedStyle(element);
           return {edge:r.left+parseFloat(s.paddingLeft),align:s.textAlign};
          };
          for(let i=2;i<h.length;i++){
           const a=check(h,i),b=check(f,i),c=check(d,i);
           if(Math.abs(a.edge-b.edge)>.1||Math.abs(a.edge-c.edge)>.1||
              a.align!=='left'||b.align!=='left'||c.align!=='left')
            throw Error('Misaligned column '+i+': '+JSON.stringify([a,b,c]));
          }
        };
        try {
          measure();
          document.querySelector('#filt details').open=true;measure();
          const field=rawFields[0];setField(field,true);renderFilters();render();measure();
          st.open.add(DATA[0].ticker);render();measure();
          document.body.dataset.layoutResult='ok';
        }catch(e){document.body.dataset.layoutResult=e.message;}
        </script>
        """
        html = html.replace("</body></html>", measure + "</body></html>")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as profile:
                for width in (700, 1400):
                    result = subprocess.run(
                        [_BROWSER, "--headless", "--disable-gpu", "--no-first-run",
                         "--disable-background-networking", f"--user-data-dir={profile}",
                         f"--window-size={width},900", "--dump-dom",
                         f"http://127.0.0.1:{server.server_port}/"],
                        capture_output=True, encoding="utf-8", timeout=45,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('data-layout-result="ok"', result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
