#!/usr/bin/env python
"""wulffhound MCP server (stdio): the deterministic Wulff tools for an agent.

Run in the wulff-dev env (Python 3.11, pymatgen, mcp):
    ./wh [--material M] server

Every tool returns a JSON object (structured content). Numbers (gamma, areas, costs) come only from the ledger and the engine;
each gamma carries its provenance (run_dir, source, cached, md5 of the data file). Failures come back as
ok False with the tool's own error and recovery hints, never as an exception the agent cannot read.
"""
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from engine import Engine  # noqa: E402
from mcp.types import TextContent  # noqa: E402

try:                                    # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except Exception:                       # 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

_AGENT = os.path.join(os.path.dirname(HERE), 'AGENT.md')
INSTRUCTIONS = open(_AGENT).read() if os.path.isfile(_AGENT) else ''
mcp = _Server('wulffhound', instructions=INSTRUCTIONS[:12000] or None)
_engines = {}


def _num(name, cast=float):
    v = os.environ.get(name, '').strip()
    return cast(v) if v and v.lower() != 'none' else None


def _eng(material):
    material = (material or os.environ.get('WULFFHOUND_MATERIAL') or 'tnt').lower()
    if material not in _engines:
        b = os.environ.get('WULFFHOUND_BUDGET_MIN', '30')
        e = Engine(material, session=os.environ.get('WULFFHOUND_SESSION', 'agent'),
                   budget_min=float(b) if b and b.lower() != 'none' else None,
                   cached_only=os.environ.get('WULFFHOUND_CACHED_ONLY', '') == '1',
                   live_runs=_num('WULFFHOUND_LIVE_RUNS', int), live_level=_num('WULFFHOUND_LIVE_LEVEL', int),
                   live_max_min=_num('WULFFHOUND_LIVE_MAX_MIN'))
        e.note_session()
        _engines[material] = e
    return _engines[material]


def _out(fn):
    """One text block of compact JSON (no structured-content wrapper, no indentation: fewer tokens)."""
    try:
        r = fn()
    except Exception as exc:
        r = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc),
             'where': traceback.format_exc().strip().splitlines()[-3:]}
    return TextContent(type='text', text=json.dumps(r, default=str, separators=(',', ':')))


@mcp.tool()
def material_info(material: str = '') -> TextContent:
    """Material, cell setting (lattice, space group, systematic absences, Miller convention), force field,
    ledger status. Read miller_setting before converting literature (hkl) into our setting."""
    return _out(lambda: _eng(material).material_info())


@mcp.tool()
def list_candidate_facets(material: str = '', n: int = 20, max_index: int = 3) -> TextContent:
    """Symmetry-distinct (hkl) families ranked by BFDH (extinction-corrected d_hkl), with the BFDH area, the hkl
    and search_range that would run (smallest known oriented cell), what the ledger already has (gamma_L1,
    gamma_L2), past failures and the estimated CPU minutes of the next level."""
    return _out(lambda: {'candidates': _eng(material).candidates(n, max_index)})


@mcp.tool()
def estimate_cost(family: str, level: int = 1, material: str = '', hkl: list[int] | None = None,
                  search_range: int | None = None, n_periods: int | None = None) -> TextContent:
    """Estimated CPU minutes of a level-1 (screen) or level-2 (relax) run of a family (e.g. "{0 1 1}"); 0 if
    an identical request is already in the ledger (cached). The basis says whether the cell size is measured."""
    return _out(lambda: _eng(material).estimate_cost(family, level, hkl, search_range, n_periods))


@mcp.tool()
def estimate_costs(families: list[str], level: int = 1, material: str = '') -> TextContent:
    """estimate_cost for several families at once (default member and search_range of each): one row per family
    with est_minutes (0 when cached), charge_minutes for cached ones, basis and notes."""
    return _out(lambda: {'level': level, 'rows': [_eng(material).estimate_cost(f, level) for f in families],
                         'budget': _eng(material).budget()})


@mcp.tool()
def screen_facets(families: list[str], material: str = '', max_minutes: float = 4.0) -> TextContent:
    """screen_facet (level 1) for several families in order, each with its default member and search_range:
    one compact row per family (gamma, cached, charged CPU min, run_dir, or the error with recovery_hints).
    Stops early when a run is refused for the budget or the live-run allowance. Use screen_facet for a specific
    hkl or search_range."""
    def f():
        eng, rows = _eng(material), []
        for fam in families:
            r = eng.run_facet(fam, 1, max_minutes=max_minutes)
            row = {k: r.get(k) for k in ('family', 'ok', 'gamma', 'cached', 'counted_as_live', 'charged_cpu_min',
                                         'cpu_s', 'hkl', 'search_range', 'n_terminations', 'faces_equivalent',
                                         'run_dir', 'refused', 'error', 'recovery_hints') if r.get(k) is not None}
            rows.append(row)
            if r.get('refused') and ('budget' in (r.get('error') or '') or 'allowance' in (r.get('error') or '')):
                break
        return {'rows': rows, 'budget': eng.budget()}
    return _out(f)


@mcp.tool()
def screen_facet(family: str, material: str = '', hkl: list[int] | None = None, search_range: int | None = None,
                 n_periods: int | None = None, max_minutes: float = 4.0) -> TextContent:
    """Level 1: unrelaxed surface energy of every termination of a family (supercell + slabcutter). Returns the
    lowest one as gamma (mJ/m^2), all terminations, provenance. Cached when an identical run exists. Refused if
    the estimate exceeds max_minutes. On failure: the tool error and recovery_hints (e.g. raise search_range)."""
    return _out(lambda: _eng(material).run_facet(family, 1, hkl, search_range, n_periods, max_minutes))


@mcp.tool()
def relax_facet(family: str, material: str = '', hkl: list[int] | None = None, search_range: int | None = None,
                n_periods: int | None = None, max_minutes: float = 6.0) -> TextContent:
    """Level 2: relaxed T = 0 surface energy (surfenergy temperature 0, lowest unrelaxed termination; bulk and
    slab minimised). Refused (not run) if the estimated CPU minutes exceed max_minutes (default 6). Cached when
    an identical run exists."""
    return _out(lambda: _eng(material).run_facet(family, 2, hkl, search_range, n_periods, max_minutes))


@mcp.tool()
def build_wulff(level: str = 'best', material: str = '', n_samples: int = 40,
                include_inconsistent: bool = False) -> TextContent:
    """Wulff shape (pymatgen) from the ledger at level "0" (BFDH), "1", "2" or "best" (level 2 where available,
    else level 1 scaled by the measured mean relaxation ratio). Returns area fractions with their spread over
    n_samples resamples of every gamma within sigma, p_on_shape, for off-shape families the gamma reduction
    needed to appear, the next most informative facets (score = relevance x gain / minutes) and the stop rule.
    Level-2 results that fail the consistency guard (relaxed > unrelaxed + 1 mJ/m^2, or molecules not intact) are
    listed under 'inconsistent' and replaced by the family's level-1 value unless include_inconsistent is True.
    material: "tnt" or "paracetamol" (default: $WULFFHOUND_MATERIAL, else tnt)."""
    def f():
        eng = _eng(material)
        lv = level if level == 'best' else int(level)
        r = eng.build_wulff(lv, n_samples=n_samples, include_inconsistent=include_inconsistent)
        r.pop('gamma_in_build', None)
        r.pop('families_in_build', None)
        for row in r.get('facets', []):
            if row.get('level') == 2:
                row.pop('basis', None)
        r['budget'] = eng.budget()
        return r
    return _out(f)


@mcp.tool()
def log_decision(action: str, why: str, expected_cost_minutes: float | None = None, outcome: str | None = None,
                 material: str = '') -> TextContent:
    """Record a decision (what you will do or did, why, the expected CPU cost, optionally the outcome). It goes
    into the report's decision log. Log every live run (and its outcome), every skip, recovery and stop; a batch
    of cached replays may share one entry that lists their outcomes."""
    return _out(lambda: _eng(material).log_decision(action, why, expected_cost_minutes, 'agent', outcome))


@mcp.tool()
def add_literature(citation: str, claim: str, doi_or_url: str = '', hkl_reported: str = '',
                   setting_conversion: str = '', hkl_our_setting: list[int] | None = None, kind: str = 'habit',
                   value_reported: str = '', material: str = '') -> TextContent:
    """Record one literature claim retrieved through MCP Bond: citation, DOI/URL, the claim, the (hkl) as the
    paper reports it, the Miller-setting conversion you did (the paper's cell vs ours, by lattice parameters)
    and the hkl in our setting. kind: habit | surface_energy | morphology_prediction | method. value_reported is
    the paper's own number as text with its units: it goes to the 'from papers' column only, never into a gamma."""
    return _out(lambda: _eng(material).add_literature(citation, claim, doi_or_url, hkl_reported or None,
                                                      setting_conversion, hkl_our_setting, kind,
                                                      value_reported or None))


@mcp.tool()
def get_report_path(material: str = '', level: str = 'best') -> TextContent:
    """Write the self-contained HTML report (3D shape, BFDH shape, facet table with provenance, decision log,
    literature) and return its path."""
    def f():
        eng = _eng(material)
        lv = level if level == 'best' else int(level)
        out = eng.write_report(level=lv)
        out['budget'] = eng.budget()
        return out
    return _out(f)


if __name__ == '__main__':
    mcp.run()
