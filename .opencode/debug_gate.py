import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import index  # noqa
from test_extractors import _IsoEnv, _run_index, _load_meta, _make_text_pdf, _touch

allowed_md = {"md", "txt"}
with _IsoEnv() as iso:
    good = iso.vault / "gone.pdf"
    _make_text_pdf(good)
    _run_index(iso)
    print("1 meta after first run:", {k: v.get("chunks") for k, v in _load_meta(iso).items()
                                      if isinstance(v, dict)})
    n_calls = len(iso.encoder.calls)
    good.unlink()
    print("2 file exists after unlink:", good.exists())
    files = index.collect_md_files(str(iso.vault), set(), set(), (), ["md", "pdf", "docx"])
    print("3 collected files:", [p.name for p in files])
    index._index_core(str(iso.vault), "col_test", iso.meta_file,
                      set(), set(), (), ["md", "pdf", "docx"],
                      600, 200, library_label="t",
                      incremental=True, full=False, tbd_ratio=0.1,
                      agent_allowed=allowed_md)
    meta = _load_meta(iso)
    print("4 meta after restricted run:", {k: v.get("chunks") for k, v in meta.items()})
    import chromadb
    col = chromadb.PersistentClient(path=str(index.CHROMA_DIR)).get_or_create_collection("col_test")
    print("5 chroma count:", col.count())
    stale, stats = index.kb_stale(str(iso.vault), meta_file=iso.meta_file,
                                  collection_name="col_test",
                                  extensions=["md", "pdf", "docx"],
                                  agent_allowed=allowed_md)
    print("6 kb_stale:", stale, stats)
