"""One Euro filter et lissage de caméra : purs, sans média."""

import itertools
import random

import pytest

from minavio.config.schema import SmoothingSettings
from minavio.core.reframe import OneEuroFilter, simplify_path, smooth_camera

FPS = 30


def series(fn, seconds=6.0, fps=FPS):
    return [(i / fps, fn(i / fps)) for i in range(int(seconds * fps))]


def test_one_euro_stable_on_noise():
    rnd = random.Random(1)
    f = OneEuroFilter(min_cutoff=0.3, beta=0.02, speed_scale=100)
    noisy = [0.5 + rnd.gauss(0, 0.02) for _ in range(300)]
    out = [f(i / FPS, x) for i, x in enumerate(noisy)]

    def std(v):
        m = sum(v) / len(v)
        return (sum((a - m) ** 2 for a in v) / len(v)) ** 0.5

    assert std(out[60:]) < 0.4 * std(noisy[60:])


def test_one_euro_follows_a_jump():
    f = OneEuroFilter(min_cutoff=0.3, beta=0.5, speed_scale=100)
    for i in range(30):
        f(i / FPS, 0.2)
    y = 0.2
    for i in range(30, 30 + 3 * FPS):
        y = f(i / FPS, 0.8)
    assert y == pytest.approx(0.8, abs=0.05)


def test_one_euro_reset():
    f = OneEuroFilter(0.3, 0.0)
    f(0.0, 0.1)
    f(0.1, 0.1)
    f.reset()
    assert f(0.2, 0.9) == 0.9


def test_dead_zone_keeps_camera_still():
    cfg = SmoothingSettings(dead_zone=0.05)
    raw = series(lambda t: 0.5 + 0.02 * (1 if int(t * 4) % 2 else -1))
    path = smooth_camera(raw, cfg)
    assert max(x for _, x in path) - min(x for _, x in path) < 1e-6


def test_dead_zone_moves_when_target_leaves():
    cfg = SmoothingSettings(dead_zone=0.05)
    path = smooth_camera(series(lambda t: 0.5 if t < 1 else 0.8), cfg)
    assert path[-1][1] > 0.7


def test_max_pan_speed_is_respected():
    cfg = SmoothingSettings(max_pan_per_s=0.2, reset_on_scene_cut=False)
    raw = series(lambda t: 0.1 if t < 1 else 0.9, fps=10)
    path = smooth_camera(raw, cfg, cuts={10})  # cut ignoré car reset désactivé
    speeds = [abs(x1 - x0) / (t1 - t0) for (t0, x0), (t1, x1) in itertools.pairwise(path)]
    assert max(speeds) <= 0.2 + 1e-6


def test_scene_cut_resets_with_hard_jump():
    cfg = SmoothingSettings(max_pan_per_s=0.2)
    raw = series(lambda t: 0.2 if t < 2 else 0.8, seconds=4, fps=4)
    path = smooth_camera(raw, cfg, cuts={8})
    t_cut = raw[8][0]
    before = [x for t, x in path if t < t_cut - 1e-4][-1]
    after = next(x for t, x in path if t >= t_cut)
    assert before == pytest.approx(0.2, abs=0.02)
    assert after == pytest.approx(0.8, abs=0.02)


def test_detection_gap_holds_then_returns_to_center():
    raw = [(i / 4, 0.8 if i < 8 else None) for i in range(60)]
    path = dict(smooth_camera(raw, SmoothingSettings()))
    held = [x for t, x in path.items() if 2.0 <= t <= 3.2]
    assert all(x == pytest.approx(0.8, abs=0.03) for x in held)
    assert 0.5 <= path[max(path)] < 0.7  # revenu vers le centre


def test_empty_and_simplify():
    assert smooth_camera([]) == [(0.0, 0.5)]
    pts = [(i / 10, 0.5 + 0.1 * (i % 2)) for i in range(400)]
    assert len(simplify_path(pts)) <= 60
