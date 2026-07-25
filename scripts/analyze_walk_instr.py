#!/usr/bin/env python3
"""Analyze dwalk work-distribution instrumentation.

Parses the [CIRCLEINSTR] and [WALKINSTR] lines emitted when a dwalk run is
launched with CIRCLE_INSTRUMENT=<seconds>, and produces the views that answer
"why did work collapse onto one rank?":

  1. Active-rank timeline, binned into time windows so every window captures
     all ranks (each rank emits on its own timer, scattered across seconds).
     This is the money plot: watch the working-rank count collapse toward 1.
  2. Per-rank lifetime totals plus each rank's last-active time -- the rank
     still working at the end is the straggler.
  3. Largest directories (fan-out) and which rank read each.

Usage:
    analyze_walk_instr.py [--window SEC] [--top N] RUN.log [RUN.log ...]
    srun ... | analyze_walk_instr.py --window 30 -
"""

import sys
import argparse
from datetime import datetime

import re

KV = re.compile(r'(\w+)=("[^"]*"|\S+)')
TS = re.compile(r'^\[([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})\]')


def parse_lines(lines):
    """Yield (kind, epoch_seconds_or_None, fields) per instrumentation line."""
    for line in lines:
        if '[CIRCLEINSTR]' in line:
            kind = 'circle'
        elif '[WALKINSTR]' in line:
            kind = 'walk'
        else:
            continue
        epoch = None
        m = TS.match(line)
        if m:
            try:
                epoch = datetime.strptime(m.group(1),
                                          "%Y-%m-%dT%H:%M:%S").timestamp()
            except ValueError:
                epoch = None
        fields = {'final': 'FINAL' in line}
        for k, v in KV.findall(line):
            fields[k] = v.strip('"')
        yield kind, epoch, fields


def i(fields, key, default=0):
    try:
        return int(fields[key])
    except (KeyError, TypeError, ValueError):
        return default


def f(fields, key, default=0.0):
    try:
        return float(fields[key])
    except (KeyError, TypeError, ValueError):
        return default


def hhmmss(epoch):
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('files', nargs='+', help='log file(s), or - for stdin')
    ap.add_argument('--window', type=int, default=30,
                    help='timeline bin width in seconds (default 30)')
    ap.add_argument('--top', type=int, default=25,
                    help='rows to show in per-rank and fan-out tables')
    args = ap.parse_args(argv[1:])

    circ = []   # (epoch, fields) for [CIRCLEINSTR]
    walk = []   # fields for [WALKINSTR]
    for p in args.files:
        fh = sys.stdin if p == '-' else open(p)
        for kind, epoch, fields in parse_lines(fh):
            if kind == 'circle':
                circ.append((epoch, fields))
            else:
                walk.append(fields)
        if p != '-':
            fh.close()

    if not circ:
        print("No [CIRCLEINSTR] lines found. Was the run launched with "
              "CIRCLE_INSTRUMENT=<seconds>?", file=sys.stderr)
        return 1

    ranks = sorted({i(fl, 'rank', -1) for _, fl in circ} - {-1})
    nranks = len(ranks)
    W = max(1, args.window)

    # rank -> hostname (present only if the build emits host=)
    host_of = {}
    for _, fl in circ:
        r = i(fl, 'rank', -1)
        if r >= 0 and fl.get('host'):
            host_of[r] = fl['host']

    # ---- bin non-final samples into (window, rank) cells -----------------
    # per cell: summed delta (progress in window), last qdepth/cbfrac seen.
    epochs = [e for e, _ in circ if e is not None]
    t0 = min(epochs) if epochs else 0
    cells = {}                 # (win, rank) -> dict
    last_active = {}           # rank -> latest window with delta>0
    for epoch, fl in circ:
        if fl['final'] or epoch is None:
            continue
        win = int((epoch - t0) // W)
        r = i(fl, 'rank', -1)
        c = cells.setdefault((win, r), {'delta': 0, 'q': 0, 'cb': 0.0})
        c['delta'] += i(fl, 'delta')
        c['q'] = i(fl, 'qdepth')
        c['cb'] = f(fl, 'cbfrac')
        if i(fl, 'delta') > 0:
            last_active[r] = max(last_active.get(r, -1), win)

    # ---- aggregate per window -------------------------------------------
    wins = {}
    for (win, r), c in cells.items():
        w = wins.setdefault(win, {'working': 0, 'holding': 0, 'present': 0,
                                  'maxq': 0, 'maxq_rank': None, 'items': 0,
                                  'cbs': []})
        w['present'] += 1
        if c['delta'] > 0:
            w['working'] += 1
            w['items'] += c['delta']
            w['cbs'].append(c['cb'])
        if c['q'] > 0:
            w['holding'] += 1
        if c['q'] > w['maxq']:
            w['maxq'] = c['q']
            w['maxq_rank'] = r

    print("=" * 92)
    print(f"VIEW 1  active-rank timeline  (window={W}s, {nranks} ranks seen)  "
          "-- watch 'working' collapse toward 1")
    print("=" * 92)
    print(f"{'time':8} {'working':>7} {'holding':>7} {'idle':>5} "
          f"{'maxq':>9} {'mq_rk':>5} {'agg_rate/s':>11} {'cbfrac~':>7}  bar(working)")
    for win in sorted(wins):
        w = wins[win]
        idle = w['present'] - w['working']
        rate = w['items'] / W
        cbmed = median(w['cbs'])
        bar = '#' * int(round(40.0 * w['working'] / max(1, nranks)))
        print(f"{hhmmss(t0 + win * W):8} {w['working']:7d} {w['holding']:7d} "
              f"{idle:5d} {w['maxq']:9d} {str(w['maxq_rank']):>5} "
              f"{rate:11.0f} {cbmed:7.2f}  {bar}")

    # ---- per-rank lifetime totals ---------------------------------------
    last = {}
    for _, fl in circ:
        r = i(fl, 'rank', -1)
        if r < 0:
            continue
        if fl['final'] or r not in last or not last[r]['final']:
            last[r] = fl

    print()
    print("=" * 92)
    print("VIEW 2  per-rank lifetime totals (sorted by processed desc); "
          "last_active = last window this rank made progress")
    print("=" * 92)
    print(f"{'rank':>5} {'host':>16} {'processed':>12} {'shared_items':>12} "
          f"{'shared_max':>10} {'recv_items':>11} {'req':>8} {'nowork':>8} "
          f"{'last_active':>11}")
    rows = sorted(last.values(), key=lambda s: i(s, 'processed'), reverse=True)
    tot_proc = 0
    for s in rows:
        tot_proc += i(s, 'processed')
    for s in rows[:args.top]:
        r = i(s, 'rank')
        la = last_active.get(r)
        la_s = hhmmss(t0 + la * W) if la is not None else '-'
        print(f"{r:5d} {host_of.get(r, '?'):>16} {i(s, 'processed'):12d} "
              f"{i(s, 'shared_items'):12d} {i(s, 'shared_max'):10d} "
              f"{i(s, 'recv_items'):11d} {i(s, 'req'):8d} {i(s, 'nowork'):8d} "
              f"{la_s:>11}")
    if rows:
        top = i(rows[0], 'processed')
        share = (100.0 * top / tot_proc) if tot_proc else 0.0
        print("-" * 92)
        print(f"total processed={tot_proc}  |  busiest rank {i(rows[0],'rank')} "
              f"did {share:.1f}% of all work")

    # ---- straggler tail: who is still working in the final windows ------
    if wins:
        last_win = max(wins)
        # find the last window where more than one rank worked
        multi = [win for win in sorted(wins) if wins[win]['working'] > 1]
        solo_start = (max(multi) + 1) if multi else min(wins)
        stragglers = sorted({r for (win, r), c in cells.items()
                             if win >= solo_start and c['delta'] > 0})

        # silent-rank detection: a rank stuck inside a process callback stops
        # emitting (the sample is at the top of the work loop). Flag ranks
        # whose last sample is well before the run's end -- these are hang
        # candidates that can block libcircle termination for everyone.
        run_end = max(e for e, _ in circ if e is not None)
        last_seen = {}
        for e, fl in circ:
            if e is None:
                continue
            r = i(fl, 'rank', -1)
            if r >= 0:
                last_seen[r] = max(last_seen.get(r, e), e)
        gap = max(5 * W, 300)
        silent = sorted((r for r, e in last_seen.items() if e < run_end - gap),
                        key=lambda r: last_seen[r])

        print()
        print("=" * 92)
        print("VIEW 2b  tail")
        print("=" * 92)
        if multi:
            print(f"work went effectively single-rank at "
                  f"{hhmmss(t0 + solo_start * W)} "
                  f"(last window with >1 worker: {hhmmss(t0 + max(multi) * W)})")
        strag_s = ", ".join(f"{r}({host_of.get(r, '?')})" for r in stragglers)
        print(f"ranks still making progress after that point: "
              f"{strag_s if stragglers else '(none)'}")
        print(f"run spans {hhmmss(t0)} .. {hhmmss(run_end)} "
              f"({(run_end - t0) / 3600.0:.2f} h of instrumented samples)")
        if silent:
            print()
            print(f"!! {len(silent)} rank(s) went SILENT >{gap}s before run end "
                  f"-- stuck in a callback, candidate hang(s) blocking termination:")
            for r in silent:
                print(f"     rank {r} on {host_of.get(r, '?')} "
                      f"last seen {hhmmss(last_seen[r])} "
                      f"(silent {(run_end - last_seen[r]) / 3600.0:.2f} h)")

    # ---- largest directories --------------------------------------------
    if walk:
        print()
        print("=" * 92)
        print("VIEW 3  largest directories (fan-out read by a single rank, "
              "uninterruptible until done)")
        print("=" * 92)
        walk.sort(key=lambda w: i(w, 'fanout'), reverse=True)
        print(f"{'fanout':>10} {'rank':>5} {'host':>16}  dir")
        for w in walk[:args.top]:
            h = w.get('host') or host_of.get(i(w, 'rank'), '?')
            print(f"{i(w, 'fanout'):10d} {i(w, 'rank'):5d} {h:>16}  "
                  f"{w.get('dir', '')}")
        # per-rank count of big dirs, to spot a rank that read many
        by_rank = {}
        for w in walk:
            by_rank[i(w, 'rank')] = by_rank.get(i(w, 'rank'), 0) + 1
        hot = sorted(by_rank.items(), key=lambda kv: kv[1], reverse=True)[:5]
        print("ranks that read the most big directories: " +
              ", ".join(f"rank {r}: {n}" for r, n in hot))

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
