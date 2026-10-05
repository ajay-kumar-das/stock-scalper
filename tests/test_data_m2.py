"""M2 data layer: candle parsing, storage round-trip, point-in-time universe, tick validation,
bar building and the bar-agreement check."""
from datetime import date, datetime, timedelta

from scalper.core.types import IST, Bar
from scalper.data.bhavcopy import UniverseRules, parse_bhavcopy, point_in_time_universe
from scalper.data.store import BarStore
from scalper.data.ticks import BarBuilder, StalenessMonitor, Tick, TickRecorder, TickValidator, bar_agreement, read_ticks
from scalper.data.upstox_history import month_windows, parse_candles

K = "NSE_EQ|INE002A01018"


def test_parse_candles_orders_chronologically_and_converts_to_paise():
    payload = {"status": "success", "data": {"candles": [
        ["2025-01-02T09:16:00+05:30", 1235.5, 1236.0, 1234.9, 1235.95, 1200, 0],
        ["2025-01-02T09:15:00+05:30", 1234.0, 1236.1, 1233.0, 1235.5, 5000, 0]]}}
    bars = parse_candles(K, payload)
    assert [b.ts.minute for b in bars] == [15, 16]
    assert bars[0].o == 123400 and bars[1].c == 123595 and bars[0].v == 5000


def test_month_windows_respect_api_limit():
    w = list(month_windows(date(2024, 1, 15), date(2024, 3, 10)))
    assert w == [(date(2024, 1, 15), date(2024, 1, 31)), (date(2024, 2, 1), date(2024, 2, 29)),
                 (date(2024, 3, 1), date(2024, 3, 10))]


def test_bar_store_round_trip(tmp_path):
    ts = datetime(2025, 1, 2, 9, 15, tzinfo=IST)
    bars = [Bar(K, ts + timedelta(minutes=i), 100_00 + i, 100_50 + i, 99_50 + i, 100_20 + i, 1000 + i) for i in range(5)]
    st = BarStore(tmp_path)
    st.write(bars)
    got = st.load_day(date(2025, 1, 2))[K]
    assert got == bars and st.dates() == [date(2025, 1, 2)]


LEGACY = """SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN
{rows}"""


def _legacy_rows(n_days, sym, px, turnover, rng_pct=0.02, series="EQ"):
    out = []
    for i in range(n_days):
        d = date(2024, 1, 1) + timedelta(days=i)
        out.append(f"{sym},{series},{px},{px*(1+rng_pct/2)},{px*(1-rng_pct/2)},{px},{px},{px},100000,{turnover},"
                   f"{d.strftime('%d-%b-%Y').upper()},5000,INE000")
    return out


def test_point_in_time_universe_has_no_lookahead_and_applies_filters():
    rows = (_legacy_rows(30, "LIQ", 1000.0, 300e7) + _legacy_rows(30, "THIN", 1000.0, 10e7)
            + _legacy_rows(30, "PENNY", 20.0, 300e7) + _legacy_rows(30, "BEX", 1000.0, 300e7, series="BE"))
    parsed = parse_bhavcopy(LEGACY.format(rows="\n".join(rows)))
    uni = point_in_time_universe(parsed, UniverseRules(min_history=15))
    days = sorted(uni)
    assert uni[days[0]] == []                     # no history on day one -> nothing eligible
    assert uni[days[14]] == []                    # 14 prior sessions < min_history
    assert uni[days[15]] == ["LIQ"]               # thin, penny and BE-series excluded


def test_udiff_format_parses():
    text = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
            "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
            "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
            "2024-07-08,2024-07-08,CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,3190.0,3210.5,"
            "3175.1,3200.4,3201.0,3185.0,,3200.4,,,5123456,16400000000.5,250000,F1,1,,,,,\n")
    r = parse_bhavcopy(text)[0]
    assert r.symbol == "RELIANCE" and r.d == date(2024, 7, 8) and r.volume == 5123456 and r.prev_close == 3185.0


def mk_tick(sec, ltp, vtt, bid=None, ask=None, ltq=10, key=K):
    ts = datetime(2025, 1, 2, 9, 15, tzinfo=IST) + timedelta(seconds=sec)
    return Tick(key, ts, ts, ltp, ltq, vtt, ltp, 0, 0, ((bid, 100, 1),) if bid else (), ((ask, 100, 1),) if ask else ())


def test_validator_quarantines_bad_data():
    v = TickValidator()
    assert v.check(mk_tick(0, 100_000, 1000, 99_995, 100_005)) is None
    assert "crossed" in v.check(mk_tick(1, 100_000, 1010, 100_010, 100_000))
    v.release(K)
    assert "decreased" in v.check(mk_tick(2, 100_000, 900))
    v.release(K)
    assert "jump" in v.check(mk_tick(3, 130_000, 1100))


def test_staleness_monitor():
    s = StalenessMonitor()
    t = mk_tick(0, 100_000, 10)
    s.on_tick(t)
    assert not s.feed_stale(t.recv_ts + timedelta(seconds=2))
    assert s.feed_stale(t.recv_ts + timedelta(seconds=4))
    assert s.stale_keys(t.recv_ts + timedelta(seconds=6), [K, "OTHER"]) == [K, "OTHER"]


def test_bar_builder_and_agreement():
    bb = BarBuilder()
    done = []
    seq = [(0, 100_000, 1_000), (20, 100_200, 1_100), (40, 99_900, 1_300), (59, 100_100, 1_400),
           (61, 100_150, 1_450), (130, 100_300, 1_600)]       # minute 9:16 has one tick; 9:17 none until 130s
    for s, p, v in seq:
        b = bb.on_tick(mk_tick(s, p, v, ltq=10))
        if b:
            done.append(b)
    done += bb.flush(datetime(2025, 1, 2, 9, 19, tzinfo=IST))
    assert [b.ts.minute for b in done] == [15, 16, 17]
    b0 = done[0]
    assert (b0.o, b0.h, b0.l, b0.c, b0.v) == (100_000, 100_200, 99_900, 100_100, 410)
    ref = [Bar(K, b.ts, b.o, b.h, b.l, b.c, b.v) for b in done]
    assert bar_agreement(done, ref)["passes"]
    ref[0] = Bar(K, ref[0].ts, ref[0].o, ref[0].h + 50, ref[0].l, ref[0].c, ref[0].v)
    assert not bar_agreement(done, ref)["passes"]


def test_tick_recorder_round_trip(tmp_path):
    rec = TickRecorder(tmp_path, flush_every=2)
    ticks = [mk_tick(i, 100_000 + i, 1000 + i, 99_990, 100_010) for i in range(5)]
    for t in ticks:
        rec.on_tick(t)
    rec.flush()
    back = read_ticks(tmp_path, date(2025, 1, 2))
    assert [t.ltp for t in back] == [t.ltp for t in ticks] and back[0].best_ask == 100_010


def test_feed_normaliser_full_mode_and_market_info():
    from scalper.data.upstox_feed import normalise
    now = datetime(2025, 1, 2, 10, 0, 0, tzinfo=IST)
    msg = {"type": "live_feed", "feeds": {K: {"fullFeed": {"marketFF": {
        "ltpc": {"ltp": 1234.5, "ltt": "1735792200000", "ltq": "25", "cp": 1220.0},
        "marketLevel": {"bidAskQuote": [{"bidQ": "100", "bidP": 1234.4, "askQ": "80", "askP": 1234.6},
                                        {"bidQ": "50", "bidP": 1234.3, "askQ": "0", "askP": 0}]},
        "atp": 1230.1, "vtt": "1500000", "tbq": 250000.0, "tsq": 310000.0}}},
        "NSE_INDEX|Nifty 50": {"fullFeed": {"indexFF": {"ltpc": {"ltp": 24000.5, "ltt": "1735792200000"}}}},
        "BAD": {"fullFeed": {"marketFF": {}}}}}
    ticks, info = normalise(msg, now)
    assert info is None and len(ticks) == 2
    t = {x.key: x for x in ticks}[K]
    assert t.ltp == 123450 and t.ltq == 25 and t.vtt == 1_500_000 and t.best_bid == 123440 and t.best_ask == 123460
    assert len(t.asks) == 1                                  # zero-price levels dropped
    _, info = normalise({"type": "market_info", "marketInfo": {"segmentStatus": {"NSE_EQ": "NORMAL_OPEN"}}}, now)
    assert info["segmentStatus"]["NSE_EQ"] == "NORMAL_OPEN"


def test_calendar_and_event_exclusions(tmp_path):
    from scalper.session.calendar import TradingCalendar, load_event_exclusions
    cal = TradingCalendar()
    assert cal.trade_allowed(date(2026, 10, 20))[0] is False                 # Dussehra
    assert cal.trade_allowed(date(2026, 11, 8))[0] is False                  # Muhurat (special) -> no trade
    assert cal.trade_allowed(date(2026, 10, 5)) == (True, "ok")
    f = tmp_path / "bm.csv"
    f.write_text("SYMBOL,COMPANY,PURPOSE,BM_DATE\nINFY,Infosys,Financial Results,16-Oct-2026\nTCS,TCS,Other business,16-Oct-2026\n")
    ex = load_event_exclusions(f)
    assert ex == {date(2026, 10, 16): {"INFY"}}


def test_session_respects_calendar_policy():
    from scalper.session.controller import SessionController
    s = SessionController()
    s.start_day(datetime(2026, 11, 8, 9, 0, tzinfo=IST), trade_allowed=(False, "special session"))
    assert s.entries_allowed(datetime(2026, 11, 8, 10, 0, tzinfo=IST))[0] is False
