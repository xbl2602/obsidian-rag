import multiprocessing as mp
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import pymupdf  # noqa

import extractors as ex  # noqa


def make_pdf(p):
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 72), "process isolated extraction works.")
    d.save(str(p))
    d.close()


if __name__ == "__main__":
    td = Path(tempfile.mkdtemp())
    p = td / "t.pdf"
    make_pdf(p)
    q = mp.Queue()
    proc = mp.Process(target=ex._preview_job, args=(q, str(p), None))
    proc.start()
    payload = q.get(timeout=60)
    proc.join(timeout=10)
    print("ok:", payload["ok"])
    info = payload.get("info", {})
    print("reason:", info.get("reason"), "| route:", info.get("route"),
          "| chars:", info.get("chars"))
    assert payload["ok"] and info["reason"] == "" and info["chars"] > 0
    print("PASS 子进程提取链路")
