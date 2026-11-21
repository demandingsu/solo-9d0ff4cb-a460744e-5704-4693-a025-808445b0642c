"""发布与观测补传的并发行为测试。

用 monkeypatch 在服务层关键位置放置“卡点”，确定性编排两种提交顺序，
不依赖时序运气：

- 发布先持锁 → 进行中的补传（JSON 与 CSV）必须 409，原始记录一条不增；
- 补传先提交 → 发布必须把新记录纳入名称解析与汇总；
- 一个批次解析物种名期间，其他批次的补传必须能完成；
- 新增名称无法解析 → 整批待核，已有结果照常可查。
"""
from __future__ import annotations

import io
import threading
import time

import app.services as services

V1_TAXONOMY = {
    "note": "v1",
    "rows": [
        {"name": "喜鹊", "higher_taxon": "雀形目",
         "accepted_name": "Pica pica", "synonyms": ["Pica serica"]},
        {"name": "Pica pica", "higher_taxon": "雀形目",
         "accepted_name": "Pica pica", "synonyms": []},
        {"name": "树麻雀", "higher_taxon": "雀形目",
         "accepted_name": "Passer montanus", "synonyms": ["麻雀"]},
        {"name": "Passer montanus", "higher_taxon": "雀形目",
         "accepted_name": "Passer montanus", "synonyms": []},
    ],
}

# 去掉喜鹊行的 v2：原名“喜鹊”在新版中无法解析
V2_TAXONOMY_NO_MAGPIE = {
    "note": "v2 移除喜鹊",
    "rows": [
        {"name": "树麻雀", "higher_taxon": "雀形目",
         "accepted_name": "Passer montanus", "synonyms": ["麻雀"]},
        {"name": "Passer montanus", "higher_taxon": "雀形目",
         "accepted_name": "Passer montanus", "synonyms": []},
    ],
}

MAGPIE_ROW = {"plot_code": "P-01", "observed_on": "2026-11-01",
              "raw_species_name": "喜鹊", "quantity": 3}
SPARROW_ROW = {"plot_code": "P-02", "observed_on": "2026-11-02",
               "raw_species_name": "麻雀", "quantity": 5}


def _create_taxonomy(client, payload=V1_TAXONOMY):
    r = client.post("/taxonomy/versions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _create_batch(client, name="并发批次"):
    r = client.post("/batches", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload_json(client, batch_id, rows):
    return client.post(f"/batches/{batch_id}/observations",
                       json={"observations": rows})


def _upload_csv(client, batch_id, csv_text):
    return client.post(
        f"/batches/{batch_id}/observations/csv",
        files={"file": ("obs.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )


def _spawn(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def test_publish_in_flight_upload_gets_409_and_adds_nothing(client, monkeypatch):
    """顺序一：发布先成功 → 仍在处理的补传 409，原始记录不得增加。"""
    _create_taxonomy(client)
    bid = _create_batch(client)
    assert _upload_json(client, bid, [MAGPIE_ROW]).status_code == 201

    # 发布进入“内存解析”阶段后卡住（此时发布持有批次锁）
    entered = threading.Event()
    release = threading.Event()
    orig_resolve_all = services.resolve_all

    def blocking_resolve_all(names, index):
        entered.set()
        assert release.wait(timeout=10)
        return orig_resolve_all(names, index)

    monkeypatch.setattr(services, "resolve_all", blocking_resolve_all)

    publish_box = {}
    t_pub = _spawn(lambda: publish_box.setdefault(
        "resp", client.post(f"/batches/{bid}/publish")))
    assert entered.wait(10), "发布未进入解析阶段"

    # 发布进行中，JSON 与 CSV 两路补传都应被拦住
    json_box, csv_box = {}, {}
    t_json = _spawn(lambda: json_box.setdefault(
        "resp", _upload_json(client, bid, [SPARROW_ROW])))
    csv_text = ("plot_code,observed_on,raw_species_name,quantity\n"
                "P-03,2026-11-03,树麻雀,2\n")
    t_csv = _spawn(lambda: csv_box.setdefault(
        "resp", _upload_csv(client, bid, csv_text)))
    time.sleep(0.3)  # 让两路补传都抵达批次锁等待
    release.set()
    for t in (t_pub, t_json, t_csv):
        t.join(10)

    pub = publish_box["resp"]
    assert pub.status_code == 200 and pub.json()["success"] is True
    assert json_box["resp"].status_code == 409
    assert csv_box["resp"].status_code == 409

    # 原始记录不得增加，发布明细只含首条记录
    obs = client.get(f"/batches/{bid}/observations").json()
    assert len(obs) == 1
    results = client.get(f"/batches/{bid}/results").json()
    assert len(results["resolved_records"]) == 1
    assert results["aggregated"][0]["total_quantity"] == 3


def test_upload_committed_first_is_included_in_publish(client, monkeypatch):
    """顺序二：补传先入库 → 发布把新增观测纳入名称解析和汇总（CSV 通道）。"""
    _create_taxonomy(client)
    bid = _create_batch(client)
    assert _upload_json(client, bid, [MAGPIE_ROW]).status_code == 201

    # 补传持锁后卡住，发布随后排队 → 发布必然读到补传提交后的快照
    inside = threading.Event()
    proceed = threading.Event()
    orig_insert = services._insert_observations

    def slow_insert(db, batch_id, items):
        inside.set()
        assert proceed.wait(timeout=10)
        return orig_insert(db, batch_id, items)

    monkeypatch.setattr(services, "_insert_observations", slow_insert)

    csv_text = ("plot_code,observed_on,raw_species_name,quantity\n"
                f"{SPARROW_ROW['plot_code']},{SPARROW_ROW['observed_on']},"
                f"{SPARROW_ROW['raw_species_name']},{SPARROW_ROW['quantity']}\n")
    upload_box = {}
    t_up = _spawn(lambda: upload_box.setdefault(
        "resp", _upload_csv(client, bid, csv_text)))
    assert inside.wait(10), "补传未进入写库阶段"

    publish_box = {}
    t_pub = _spawn(lambda: publish_box.setdefault(
        "resp", client.post(f"/batches/{bid}/publish")))
    time.sleep(0.3)  # 让发布抵达批次锁等待
    proceed.set()
    t_up.join(10)
    t_pub.join(10)

    up = upload_box["resp"]
    assert up.status_code == 201 and up.json()["inserted"] == 1
    pub = publish_box["resp"]
    assert pub.status_code == 200 and pub.json()["success"] is True

    # 两条原始记录全部进入发布明细与汇总
    results = client.get(f"/batches/{bid}/results").json()
    assert len(results["resolved_records"]) == 2
    agg = {(a["plot_code"], a["accepted_name"]): a["total_quantity"]
           for a in results["aggregated"]}
    assert agg == {("P-01", "Pica pica"): 3,
                   ("P-02", "Passer montanus"): 5}


def test_other_batch_upload_completes_while_publish_is_resolving(
        client, monkeypatch):
    """批次A 正在解析物种名时，批次B 的补传必须能完成（不被拖住）。"""
    _create_taxonomy(client)
    bid_a = _create_batch(client, "批次A")
    bid_b = _create_batch(client, "批次B")
    assert _upload_json(client, bid_a, [MAGPIE_ROW]).status_code == 201

    entered = threading.Event()
    release = threading.Event()
    orig_resolve_all = services.resolve_all

    def blocking_resolve_all(names, index):
        entered.set()
        assert release.wait(timeout=10)
        return orig_resolve_all(names, index)

    monkeypatch.setattr(services, "resolve_all", blocking_resolve_all)

    publish_box = {}
    t_pub = _spawn(lambda: publish_box.setdefault(
        "resp", client.post(f"/batches/{bid_a}/publish")))
    assert entered.wait(10), "发布未进入解析阶段"

    done = threading.Event()
    upload_box = {}

    def upload_b():
        upload_box["resp"] = _upload_json(client, bid_b, [SPARROW_ROW])
        done.set()

    t_up = _spawn(upload_b)
    try:
        # A 的解析还被卡着，B 的补传必须已经完成
        assert done.wait(5), "批次B 的补传被批次A 的发布阻塞"
    finally:
        release.set()
    t_up.join(10)
    t_pub.join(10)

    assert upload_box["resp"].status_code == 201
    assert upload_box["resp"].json()["inserted"] == 1
    assert publish_box["resp"].json()["success"] is True


def test_concurrent_publish_and_upload_never_loses_committed_records(client):
    """同批并发发布+补传的不变量：要么 409 零插入，要么 201 且发布含新记录。"""
    _create_taxonomy(client)
    for i in range(10):
        bid = _create_batch(client, f"竞态批次{i}")
        assert _upload_json(client, bid, [MAGPIE_ROW]).status_code == 201

        barrier = threading.Barrier(2)
        publish_box, upload_box = {}, {}

        def do_publish():
            barrier.wait(10)
            publish_box["resp"] = client.post(f"/batches/{bid}/publish")

        def do_upload():
            barrier.wait(10)
            upload_box["resp"] = _upload_json(client, bid, [SPARROW_ROW])

        t1 = _spawn(do_publish)
        t2 = _spawn(do_upload)
        t1.join(10)
        t2.join(10)

        up = upload_box["resp"]
        assert publish_box["resp"].status_code == 200
        results = client.get(f"/batches/{bid}/results").json()
        obs = client.get(f"/batches/{bid}/observations").json()
        if up.status_code == 201:
            # 补传先入库：发布必须包含新记录
            assert len(obs) == 2
            assert len(results["resolved_records"]) == 2
            names = {r["raw_species_name"] for r in results["resolved_records"]}
            assert names == {"喜鹊", "麻雀"}
        else:
            # 发布先成功：补传 409 且原始记录未增加
            assert up.status_code == 409
            assert len(obs) == 1
            assert len(results["resolved_records"]) == 1


def test_unresolvable_supplement_keeps_review_status_and_old_results(client):
    """新增名称无法解析：整批仍标待核，已有结果照常可查。"""
    _create_taxonomy(client)
    bid = _create_batch(client)
    assert _upload_json(client, bid, [MAGPIE_ROW]).status_code == 201
    assert client.post(f"/batches/{bid}/publish").json()["success"] is True

    # v2 移除喜鹊 → 批次 STALE；用 v2 重发布失败 → NEEDS_REVIEW，旧结果保留
    _create_taxonomy(client, V2_TAXONOMY_NO_MAGPIE)
    assert client.get(f"/batches/{bid}").json()["status"] == "STALE"
    failed = client.post(f"/batches/{bid}/publish").json()
    assert failed["success"] is False
    assert failed["status"] == "NEEDS_REVIEW"

    # 待核批次允许补传：一条可解析（麻雀）+ 一条未知名（火星潜鸟）
    r = _upload_json(client, bid, [
        SPARROW_ROW,
        {"plot_code": "P-03", "observed_on": "2026-11-03",
         "raw_species_name": "火星潜鸟", "quantity": 1},
    ])
    assert r.status_code == 201 and r.json()["inserted"] == 2

    # 重发布：新增未知名仍让整批待核
    again = client.post(f"/batches/{bid}/publish").json()
    assert again["success"] is False
    assert again["status"] == "NEEDS_REVIEW"
    issue_names = {i["raw_species_name"] for i in again["issues"]}
    assert {"喜鹊", "火星潜鸟"} <= issue_names

    # 已有结果照常可查：仍是上一份完整版本（只有喜鹊一条）
    results = client.get(f"/batches/{bid}/results").json()
    assert results["status"] == "NEEDS_REVIEW"
    assert len(results["resolved_records"]) == 1
    assert results["aggregated"][0]["accepted_name"] == "Pica pica"
    assert results["aggregated"][0]["total_quantity"] == 3
