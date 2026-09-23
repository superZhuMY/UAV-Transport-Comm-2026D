# -*- coding: utf-8 -*-
"""P1a：坐标统一与航段几何。

- 水平直线在 WGS 84 / UTM 49N 米制坐标下定义，并用大地线距离交叉核对；
- 沿线最高地形按原始 DEM 像元穿格计算：栅格 geo key 为 RasterPixelIsPoint，
  像元中心即 (x0 + j*sx, y0 - i*sy)；对每个采样点取索引空间中最邻近的 2×2
  像元块求并集，属于保守穿格（只会把最高地形取高，不会漏掉狭窄高像元）。
"""
import os
import json
import math
import numpy as np
import tifffile

from data_io import DEM_TIF, CACHE_DIR

try:
    from pyproj import Transformer, Geod
    _HAS_PYPROJ = True
except Exception:                                    # pragma: no cover
    _HAS_PYPROJ = False

CRS_UTM = 'EPSG:32649'                               # WGS 84 / UTM zone 49N
CRUISE_CLEARANCE_M = 50.0                            # 巡航海拔 = 最高地形 + 50 m


def _read_dem():
    with tifffile.TiffFile(DEM_TIF) as tf:
        page = tf.pages[0]
        dem = np.asarray(page.asarray(), dtype=np.float64)
        tags = {t.name: t.value for t in page.tags.values()}
        scale = tags['ModelPixelScaleTag']
        tie = tags['ModelTiepointTag']
        geokeys = tags.get('GeoKeyDirectoryTag')
    sx, sy = float(scale[0]), float(scale[1])
    x0, y0 = float(tie[3]), float(tie[4])            # tiepoint 指向 (0,0) 像元
    raster_type = 2
    if geokeys:
        n = geokeys[3]                               # GeoKey 条数
        for k in range(n):
            kid, loc, cnt, val = geokeys[4 + 4 * k:4 + 4 * k + 4]
            if kid == 1025:                          # GTRasterTypeGeoKey
                raster_type = val
    return dem, sx, sy, x0, y0, raster_type


class Geometry(object):
    """航段几何与穿格结果的容器。"""

    def __init__(self, nodes, step_m=1.5):
        self.nodes = nodes
        self.step_m = step_m
        self.dem, self.sx, self.sy, self.x0, self.y0, self.raster_type = _read_dem()
        self.nrow, self.ncol = self.dem.shape
        self.pixel_is_point = (self.raster_type == 2)
        if _HAS_PYPROJ:
            self.fwd = Transformer.from_crs('EPSG:4326', CRS_UTM, always_xy=True)
            self.inv = Transformer.from_crs(CRS_UTM, 'EPSG:4326', always_xy=True)
            self.geod = Geod(ellps='WGS84')
        ids = sorted(nodes)
        lon = np.array([nodes[i]['lon'] for i in ids])
        lat = np.array([nodes[i]['lat'] for i in ids])
        if _HAS_PYPROJ:
            x, y = self.fwd.transform(lon, lat)
        else:                                        # 仅退化用途的局部近似
            R = 6378137.0
            x = np.radians(lon - lon.mean()) * R * math.cos(math.radians(lat.mean()))
            y = np.radians(lat) * R
        self.node_ids = ids
        self.xy = {i: (float(x[k]), float(y[k])) for k, i in enumerate(ids)}
        self.seg = {}

    # ---------- 水平距离 ----------
    def distance(self, a, b):
        (xa, ya), (xb, yb) = self.xy[a], self.xy[b]
        d_utm = math.hypot(xb - xa, yb - ya)
        if _HAS_PYPROJ:
            lon1, lat1 = self.inv.transform(xa, ya)
            lon2, lat2 = self.inv.transform(xb, yb)
            _, _, d_geod = self.geod.inv(lon1, lat1, lon2, lat2)
        else:
            d_geod = d_utm
        return d_utm, d_geod

    # ---------- 沿线最高地形 ----------
    def max_terrain(self, a, b):
        (xa, ya), (xb, yb) = self.xy[a], self.xy[b]
        d = math.hypot(xb - xa, yb - ya)
        n = max(2, int(math.ceil(d / self.step_m)) + 1)
        t = np.linspace(0.0, 1.0, n)
        xs = xa + t * (xb - xa)
        ys = ya + t * (yb - ya)
        if _HAS_PYPROJ:
            lon, lat = self.inv.transform(xs, ys)
        else:
            lon, lat = xs, ys
        # 像元中心配准：u = (lon-x0)/sx 为像元索引坐标，像元 k 的中心在 u=k
        u = (np.asarray(lon) - self.x0) / self.sx
        v = (self.y0 - np.asarray(lat)) / self.sy
        if not self.pixel_is_point:                  # PixelIsArea：角点配准，中心 +0.5
            u = u - 0.5
            v = v - 0.5
        j0 = np.floor(u).astype(np.int64)
        i0 = np.floor(v).astype(np.int64)
        out = (i0 < -1) | (i0 + 1 > self.nrow - 1) | (j0 < -1) | (j0 + 1 > self.ncol - 1)
        n_outside = int(out.sum())
        np.clip(i0, 0, self.nrow - 2, out=i0)
        np.clip(j0, 0, self.ncol - 2, out=j0)
        blk = self.dem[i0[:, None, None] + np.arange(2)[None, :, None],
                       j0[:, None, None] + np.arange(2)[None, None, :]]
        blk = blk.reshape(n, 4)
        k = int(np.argmax(blk.max(axis=1)))
        hmax = float(blk[k].max())
        # 统计覆盖到的独立像元数（用于可复核记录）
        pidx = set()
        for di in (0, 1):
            for dj in (0, 1):
                pidx.update(zip((i0 + di).tolist(), (j0 + dj).tolist()))
        info = dict(max_z=hmax, n_samples=n, n_pixels=len(pidx), n_outside=n_outside,
                    argmax_xy=(float(xs[k]), float(ys[k])), length_m=d)
        return info

    # ---------- 240 条有向航段 ----------
    def build_segments(self, cache_key=None, verbose=True):
        if cache_key:
            cp = os.path.join(CACHE_DIR, 'segments_%s.json' % cache_key)
            if os.path.exists(cp):
                with open(cp, encoding='utf-8') as f:
                    data = json.load(f)
                if abs(data.get('step_m', -1) - self.step_m) < 1e-9:
                    self.seg = {k: v for k, v in data['segments'].items()}
                    if verbose:
                        print('[P1a] 命中航段几何缓存 %d 条' % len(self.seg))
                    return self.seg
        else:
            cp = None
        ids = self.node_ids
        info = {}
        for ia in range(len(ids)):
            for ib in range(ia + 1, len(ids)):
                a, b = ids[ia], ids[ib]
                info[(a, b)] = self.max_terrain(a, b)
        conflicts = []
        for i in ids:
            for j in ids:
                if i == j:
                    continue
                key = (i, j) if (i, j) in info else (j, i)
                g = info[key]
                d_utm, d_geod = self.distance(i, j)
                H = g['max_z'] + CRUISE_CLEARANCE_M
                zi = self.nodes[i]['work_z']
                zj = self.nodes[j]['work_z']
                h_up, h_dn = H - zi, H - zj
                if h_up < -1e-9 or h_dn < -1e-9:
                    conflicts.append((i, j, zi, zj, H))
                self.seg['%s|%s' % (i, j)] = dict(
                    i=i, j=j, d_m=d_utm, d_geod_m=d_geod, max_z_m=g['max_z'],
                    cruise_z_m=H, h_up_m=max(h_up, 0.0), h_dn_m=max(h_dn, 0.0),
                    z_work_i=zi, z_work_j=zj, n_samples=g['n_samples'],
                    n_pixels=g['n_pixels'], n_outside=g['n_outside'])
        self.conflicts = conflicts
        if verbose:
            dd = [abs(v['d_m'] - v['d_geod_m']) / max(v['d_m'], 1e-9) for v in self.seg.values()]
            print('[P1a] 航段几何：%d 条有向航段，UTM 距离与大地线距离最大相对差 %.4f%%'
                  % (len(self.seg), 100 * max(dd)))
            if conflicts:
                print('[P1a] 警告：%d 条航段的巡航海拔低于作业高度，需核查节点表与 DEM 口径：%s'
                      % (len(conflicts), conflicts[:5]))
        if cp:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cp, 'w', encoding='utf-8') as f:
                json.dump(dict(step_m=self.step_m, pixel_is_point=self.pixel_is_point,
                               segments=self.seg), f)
        return self.seg

    def seg_of(self, i, j):
        return self.seg['%s|%s' % (i, j)]

    # ---------- 验收 ----------
    def verification(self):
        ids = self.node_ids
        max_sym = 0.0
        max_swap = 0.0
        bad = []
        for i in ids:
            for j in ids:
                if i == j:
                    continue
                s1, s2 = self.seg_of(i, j), self.seg_of(j, i)
                max_sym = max(max_sym, abs(s1['d_m'] - s2['d_m']))
                max_sym = max(max_sym, abs(s1['max_z_m'] - s2['max_z_m']))
                max_swap = max(max_swap, abs(s1['h_up_m'] - s2['h_dn_m']),
                               abs(s1['h_dn_m'] - s2['h_up_m']))
                if s1['n_outside'] > 0 or s1['max_z_m'] is None:
                    bad.append((i, j))
        return dict(n_segments=len(self.seg), max_symmetry_gap_m=max_sym,
                    max_climb_descent_swap_gap_m=max_swap, out_of_coverage=bad)


if __name__ == '__main__':
    from data_io import load_instance, input_hash
    inst = load_instance(verbose=False)
    geo = Geometry(inst['nodes'])
    print('DEM: shape=%s pixel_is_point=%s origin=(%.6f, %.6f) scale=(%.3e, %.3e)'
          % ((geo.nrow, geo.ncol), geo.pixel_is_point, geo.x0, geo.y0, geo.sx, geo.sy))
    geo.build_segments(cache_key=input_hash())
    v = geo.verification()
    print('验收:', json.dumps({k: (vv if not isinstance(vv, list) else vv[:3])
                              for k, vv in v.items()}, ensure_ascii=False))
    print('示例航段 O01|S002:', json.dumps(geo.seg_of('O01', 'S002'), ensure_ascii=False))
    print('示例航段 S002|O01:', json.dumps(geo.seg_of('S002', 'O01'), ensure_ascii=False))
