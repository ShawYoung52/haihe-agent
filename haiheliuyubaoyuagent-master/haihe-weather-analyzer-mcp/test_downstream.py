import sys, json
sys.path.insert(0, r'E:/tj/line')
from rest_api import _query_downstream_rivers
for r in ['永定河','北运河','子牙河','大清河','滹沱河','漳河','卫河']:
    d = _query_downstream_rivers(r)
    direct = [x['name'] for x in d.get('direct_downstream',[])]
    indirect = ["{}(L{})".format(x['name'],x['level']) for x in d.get('indirect_downstream',[])]
    print(f'{r}: direct={direct}')
    print(f'   indirect={indirect}')
