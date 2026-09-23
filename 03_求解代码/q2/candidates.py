# -*- coding: utf-8 -*-
"""P2/P3：候选架次生成与安全剪枝。

候选只固定“一趟任务的机型 + 停靠事件 + 各站货箱”，是否选用、开始时刻和资源占用
交给联合调度模型。生成来源：
  1) 单箱直送（保证每个货箱都有覆盖候选，使模型一定可行）
  2) 区内紧急组批（该区全部硬时限货箱）
  3) 区内优先级装箱链 + 多种序列的随机化装箱变体（含第一问式整区拆分）
  4) 跨区两点、三点合并（近邻图 + 时限类别相容），含“整区装 + 邻区紧急箱”变体
所有候选都通过 simulate_route 重新评价；最晚开始时刻为负的直接删除。
"""
import math
import random
from simulator import simulate_route


def _sort_key(b):
    """组批优先级：先硬时限（截止升序），再应急优先系数降序，再质量降序。"""
    hard = b['hard_deadline_s']
    return (0 if hard is not None else 1, hard if hard is not None else 1e9,
            -b['priority'], -b['mass_g'])


class Pool(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.items = []
        self._seen = {}

    def add(self, type_id, stops, inst, geo, box_index, source):
        key = (type_id, tuple((z, tuple(sorted(ids))) for z, ids in stops))
        if key in self._seen:
            return None
        self._seen[key] = True
        ev = simulate_route(type_id, stops, inst, geo, box_index)
        if not ev['feasible']:
            return None
        latest = None
        for _, ids in stops:
            for b in ids:
                d = box_index[b]['hard_deadline_s']
                if d is None:
                    continue
                slack = d - math.ceil(ev['delivery_offsets'][b])
                latest = slack if latest is None else min(latest, slack)
        if latest is not None and latest < 0:
            return None
        rec = dict(
            candidate_id=len(self.items), type_id=type_id,
            stops=[(z, tuple(ids)) for z, ids in stops],
            box_ids=tuple(b for _, ids in stops for b in ids),
            n_boxes=len(ev['delivery_offsets']),
            duration_s=ev['duration_s'], energy_kwh=ev['energy_kwh'],
            recharge_s=ev['recharge_s'], return_soc=ev['return_soc'],
            offsets=dict(ev['delivery_offsets']), latest_start_s=latest,
            initial_mass_kg=ev['initial_mass_kg'],
            initial_volume_m3=ev['initial_volume_m3'],
            max_offset_s=max(ev['delivery_offsets'].values()), source=source)
        self.items.append(rec)
        return rec

    # ---------- 剪枝：同机型、同货箱集合的支配删除 ----------
    def prune_dominated(self):
        groups = {}
        for c in self.items:
            groups.setdefault((c['type_id'], frozenset(c['box_ids'])), []).append(c)
        keep, dropped = [], 0
        for _, lst in groups.items():
            for c in lst:
                dom = False
                for o in lst:
                    if o is c:
                        continue
                    if (o['max_offset_s'] <= c['max_offset_s']
                            and o['duration_s'] <= c['duration_s']
                            and o['duration_s'] + o['recharge_s']
                            <= c['duration_s'] + c['recharge_s']
                            and o['energy_kwh'] <= c['energy_kwh'] + 1e-9
                            and (o['max_offset_s'] < c['max_offset_s']
                                 or o['duration_s'] < c['duration_s']
                                 or o['energy_kwh'] < c['energy_kwh'] - 1e-9
                                 or o['duration_s'] + o['recharge_s']
                                 < c['duration_s'] + c['recharge_s'])):
                        dom = True
                        break
                if dom:
                    dropped += 1
                else:
                    keep.append(c)
        keep.sort(key=lambda c: (c['type_id'], c['box_ids'], c['duration_s']))
        for k, c in enumerate(keep):
            c['candidate_id'] = k
        self.items = keep
        return dropped


def _chain_fill(type_id, zone, ordered_boxes, inst, geo, box_index, pool, source):
    """按给定序列贪心装箱；返回未被装下的货箱序列。"""
    chosen, rejected = [], []
    for b in ordered_boxes:
        trial = chosen + [b]
        ev = simulate_route(type_id, [(zone, [x['box_id'] for x in trial])],
                            inst, geo, box_index)
        if ev['feasible']:
            chosen = trial
        else:
            rejected.append(b)
    if chosen:
        pool.add(type_id, [(zone, [x['box_id'] for x in chosen])], inst, geo,
                 box_index, source)
    return rejected


def _zone_chains(zone, boxes_of_zone, inst, geo, box_index, pool, types,
                 n_random=10, rng=None):
    """区内装箱链：确定性优先级链 + 多种随机序列变体。"""
    rng = rng or random.Random(0)
    for g in types:
        rem = sorted(boxes_of_zone, key=_sort_key)
        round_no = 0
        while rem and round_no < 8:
            rem = _chain_fill(g, zone, rem, inst, geo, box_index, pool, 'zone_chain')
            round_no += 1
        base = sorted(boxes_of_zone, key=_sort_key)
        for _ in range(n_random):
            order = list(base)
            rng.shuffle(order)
            # 硬时限货箱优先纳入，其余随机化，保证变体仍围绕紧急箱构造
            order.sort(key=lambda b: 0 if b['hard_deadline_s'] is not None else 1)
            rem = order
            r2 = 0
            while rem and r2 < 8:
                rem = _chain_fill(g, zone, rem, inst, geo, box_index, pool,
                                  'zone_chain_rand')
                r2 += 1


def _fill_slots(type_id, slots, inst, geo, box_index, pool, source,
                fill_pool=None, reverse=True):
    """slots: [(zone, [必装箱号]), ...]；其余容量按停靠点倒序贪心补装。"""
    stops = [[z, list(ids)] for z, ids in slots]
    if fill_pool:
        order = list(fill_pool)
        if reverse:
            order = order[::-1]
        for b in order:
            placed = False
            for zone, ids in reversed(stops):
                if b['zone_id'] != zone:
                    continue
                ids.append(b['box_id'])
                trial = [(z, list(v)) for z, v in stops]
                ev = simulate_route(type_id, trial, inst, geo, box_index)
                if ev['feasible']:
                    placed = True
                    break
                ids.pop()
            if not placed:
                continue
    return pool.add(type_id, [(z, list(v)) for z, v in stops], inst, geo,
                    box_index, source)


def build_pool(inst, geo, cfg, verbose=True):
    types = inst['types']
    boxes = inst['boxes']
    type_ids = sorted(types)
    box_index = {b['box_id']: b for b in boxes}
    by_zone = {}
    for b in boxes:
        by_zone.setdefault(b['zone_id'], []).append(b)
    zones = sorted(by_zone)
    pool = Pool(cfg)
    rng = random.Random(cfg['random_seed'])

    # 1) 单箱直送
    for b in boxes:
        for g in type_ids:
            pool.add(g, [(b['zone_id'], [b['box_id']])], inst, geo, box_index, 'single')

    def urgent_of(z):
        return [b for b in sorted(by_zone[z], key=_sort_key)
                if b['hard_deadline_s'] is not None]

    # 2) 区内紧急组批
    for z in zones:
        u = urgent_of(z)
        for g in type_ids:
            if u:
                _fill_slots(g, [(z, [b['box_id'] for b in u])], inst, geo, box_index,
                            pool, 'urgent_bundle',
                            fill_pool=[b for b in by_zone[z] if b not in u and
                                       b['hard_deadline_s'] is None])

    # 3) 区内装箱链（含随机变体）
    for z in zones:
        _zone_chains(z, by_zone[z], inst, geo, box_index, pool, type_ids,
                     n_random=10, rng=rng)

    # 4) 跨区两点 / 三点合并
    dist = {i: sorted([j for j in zones if j != i],
                      key=lambda j: geo.seg_of(i, j)['d_m']) for i in zones}
    ddl = {z: (min([b['hard_deadline_s'] for b in by_zone[z]
                    if b['hard_deadline_s'] is not None] or [1e9])) for z in zones}
    k = cfg['nearest_neighbors']

    def compat(i, j):
        d1, d2 = ddl[i], ddl[j]
        if d1 >= 1e8 or d2 >= 1e8:
            return True
        return abs(d1 - d2) <= 7200.0

    def slots_urgent(seq):
        out = []
        for z in seq:
            u = urgent_of(z)
            ids = [b['box_id'] for b in u] or [sorted(by_zone[z], key=_sort_key)[0]['box_id']]
            out.append((z, ids))
        return out

    pair_seen = set()
    for i in zones:
        for j in dist[i][:k]:
            key = tuple(sorted((i, j)))
            if key in pair_seen or not compat(i, j):
                continue
            pair_seen.add(key)
            for order in ((i, j), (j, i)):
                base = slots_urgent(order)
                for g in type_ids:
                    r1 = [b for b in by_zone[order[0]] if b['hard_deadline_s'] is None]
                    r2 = [b for b in by_zone[order[1]] if b['hard_deadline_s'] is None]
                    _fill_slots(g, base, inst, geo, box_index, pool, 'pair',
                                fill_pool=sorted(r1, key=_sort_key) +
                                sorted(r2, key=_sort_key))
                    # 变体：先装满第一个停靠点整区，再补第二个停靠点
                    if len(by_zone[order[0]]) <= 8:
                        full = [(order[0], [b['box_id'] for b in
                                            sorted(by_zone[order[0]], key=_sort_key)])]
                        full.append((order[1], [b['box_id'] for b in urgent_of(order[1])]
                                     or [sorted(by_zone[order[1]], key=_sort_key)[0]['box_id']]))
                        _fill_slots(g, full, inst, geo, box_index, pool, 'pair_full',
                                    fill_pool=sorted(
                                        [b for b in by_zone[order[1]]
                                         if b['hard_deadline_s'] is None], key=_sort_key))

    for i in zones:
        nbrs = [j for j in dist[i][:k + 1] if compat(i, j)][:4]
        for a in range(len(nbrs)):
            for b_ in range(a + 1, len(nbrs)):
                seq = (i, nbrs[a], nbrs[b_])
                for order in (seq, seq[::-1]):
                    base = slots_urgent(order)
                    for g in type_ids:
                        rests = []
                        for z in order:
                            rests += [b for b in by_zone[z] if b['hard_deadline_s'] is None]
                        _fill_slots(g, base, inst, geo, box_index, pool, 'triple',
                                    fill_pool=sorted(rests, key=_sort_key))

    dropped = pool.prune_dominated()
    if len(pool.items) > cfg['candidate_limit']:
        keep = [c for c in pool.items if c['source'] in
                ('single', 'urgent_bundle')]
        rest = [c for c in pool.items if c['source'] not in ('single', 'urgent_bundle')]
        rest.sort(key=lambda c: (c['energy_kwh'] / max(c['n_boxes'], 1)))
        pool.items = keep + rest[:cfg['candidate_limit'] - len(keep)]
        for kk, c in enumerate(pool.items):
            c['candidate_id'] = kk
    if verbose:
        from collections import Counter
        cnt = Counter(c['source'] for c in pool.items)
        back = Counter(c['type_id'] for c in pool.items)
        cover, hard_cover = set(), set()
        for c in pool.items:
            cover.update(c['box_ids'])
            for b in c['box_ids']:
                if box_index[b]['hard_deadline_s'] is not None:
                    hard_cover.add(b)
        print('[P2] 候选池：%d 条（支配剪枝删除 %d 条）' % (len(pool.items), dropped))
        print('     来源分布:', dict(cnt))
        print('     机型分布:', dict(back))
        print('     覆盖货箱 %d/80，其中硬时限货箱 %d/31' % (len(cover), len(hard_cover)))
        multi = [c for c in pool.items if len(c['stops']) > 1]
        print('     多点候选（≥2 停靠）%d 条，最多停靠 %d 站'
              % (len(multi), max(len(c['stops']) for c in pool.items)))
        avg_e = sum(c['energy_kwh'] for c in pool.items) / len(pool.items)
        print('     平均单候选能耗 %.3f kWh' % avg_e)
    return pool, box_index, by_zone


if __name__ == '__main__':
    from data_io import load_instance, input_hash, load_config
    from geometry import Geometry
    inst = load_instance(verbose=False)
    geo = Geometry(inst['nodes'])
    geo.build_segments(cache_key=input_hash(), verbose=False)
    pool, box_index, by_zone = build_pool(inst, geo, load_config())
