"""wulffhound locations: the repository, the committed replay bundle, the local state, and the (optional)
simulation chain. Everything is configurable through the environment; nothing points at a user's home.

  WULFFHOUND_STATE       local state (ledger cache, sessions, reports, run folders); default <repo>/state
  WULFFHOUND_DATA        the replay bundle (data/<material>/); default <repo>/data
  WULFFHOUND_CHAIN_ROOT  the simulation chain (surface-energy-sim2ls: supercell, slabcutter, surfenergy, cif2lammps);
                         default $SIM2L_DEV_ROOT, else ../surface-energy-sim2ls next to this clone
  WULFFHOUND_CHAIN_ENV   the chain's Python env (Sim2L env); default ~/micromamba/envs/cif2lammps-dev, else
                         <chain>/.sim2l/env

Replay (no chain) works from the bundle alone; live runs need the chain.
"""
import os

PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG)
DATA = os.path.abspath(os.environ.get('WULFFHOUND_DATA') or os.path.join(ROOT, 'data'))
STATE = os.path.abspath(os.environ.get('WULFFHOUND_STATE') or os.path.join(ROOT, 'state'))

NO_CHAIN_MSG = ('live runs need the simulation chain: surface-energy-sim2ls (private for now) or the nanoHUB tools; '
                'replay works without it (set WULFFHOUND_CHAIN_ROOT to a chain checkout and WULFFHOUND_CHAIN_ENV to '
                'its Sim2L env)')


def chain_root():
    r = os.environ.get('WULFFHOUND_CHAIN_ROOT') or os.environ.get('SIM2L_DEV_ROOT') or \
        os.path.join(os.path.dirname(ROOT), 'surface-energy-sim2ls')
    return os.path.abspath(os.path.expanduser(r))


def chain_env():
    if os.environ.get('WULFFHOUND_CHAIN_ENV'):
        return os.path.abspath(os.path.expanduser(os.environ['WULFFHOUND_CHAIN_ENV']))
    dev = os.path.expanduser('~/micromamba/envs/cif2lammps-dev')
    local = os.path.join(chain_root(), '.sim2l', 'env')
    return dev if os.path.isdir(dev) or not os.path.isdir(local) else local


def chain_present():
    """True when the chain checkout and its env are there (a live run can start)."""
    r = chain_root()
    return (os.path.isfile(os.path.join(r, 'devtools', 'sim2l-env'))
            and os.path.isdir(os.path.join(r, 'surfenergy', 'simtool'))
            and os.path.isdir(chain_env()))


def resolve(ref):
    """'chain:<path>' -> under the chain root, 'data:<path>' -> under the bundle dir, else as given."""
    if not ref:
        return ref
    if ref.startswith('chain:'):
        return os.path.join(chain_root(), ref[len('chain:'):])
    if ref.startswith('data:'):
        return os.path.join(DATA, ref[len('data:'):])
    return ref


def portable(path):
    """A path as a provenance string that names no machine: 'chain:<rel>' under the chain root, 'state:<rel>'
    under the state dir, '<rel>' under the repository; anything else is kept as its base name."""
    if not path or not isinstance(path, str):
        return path
    if ':' in path.split('/')[0]:
        return path
    p = os.path.abspath(path) if os.path.isabs(path) else None
    if p is None:
        return path
    for prefix, base in (('state:', STATE), ('chain:', chain_root()), ('', ROOT)):
        r = os.path.relpath(p, base)
        if not r.startswith('..'):
            return prefix + r
    return 'external:' + os.path.basename(p)
