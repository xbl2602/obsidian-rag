import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import config as cfgmod
import extractors as ex
from test_extractors import _FakeRequests, _make_scanned_pdf

td = Path(tempfile.mkdtemp())
p = td / "scan.pdf"
_make_scanned_pdf(p)

fake = _FakeRequests()
saved_req = sys.modules.get("requests")
sys.modules["requests"] = fake
cfgmod.CFG["pdf_scan_backend"] = "mineru-cloud"
cfgmod.CFG["mineru_api_key"] = "test-key"
try:
    md, reason = ex.extract_to_markdown(p)
    print("happy:", reason, repr((md or "")[:40]))
    print("calls:", fake.calls)
finally:
    if saved_req:
        sys.modules["requests"] = saved_req
    else:
        sys.modules.pop("requests", None)
    cfgmod.CFG.pop("pdf_scan_backend", None)
    cfgmod.CFG.pop("mineru_api_key", None)

# 失败折叠
fake2 = _FakeRequests()
fake2.fail_post = TimeoutError
sys.modules["requests"] = fake2
cfgmod.CFG["pdf_scan_backend"] = "mineru-cloud"
cfgmod.CFG["mineru_api_key"] = "k"
try:
    md2, reason2 = ex.extract_to_markdown(p)
    print("fail-fold:", reason2)
finally:
    sys.modules.pop("requests", None)
    cfgmod.CFG.pop("pdf_scan_backend", None)
    cfgmod.CFG.pop("mineru_api_key", None)
