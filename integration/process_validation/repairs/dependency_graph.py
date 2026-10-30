"""Build typed producer/consumer edges and free-gripper boundaries."""
from .dependency_rules import PAIR_KINDS, KNOWN_ACTIONS, component_key, facts_for


def build_graph(plan):
    if not isinstance(plan, list) or not plan:
        raise ValueError('协议为空，不能建立依赖')
    ids, held, pairs, nodes, edges, producers = set(), None, {}, [], [], {}
    for index, step in enumerate(plan):
        if (not isinstance(step, dict) or not isinstance(step.get('step_id'), str)
                or not step['step_id'] or step['step_id'] in ids or step.get('action') not in KNOWN_ACTIONS):
            raise ValueError('协议含缺失/重复步骤或未登记动作，不能规划局部重跑')
        step_id, action = step['step_id'], step['action']
        ids.add(step_id)
        nodes.append({'step_id': step_id, 'action': action, **facts_for(step)})
        for fact in nodes[-1]['requires']:
            if fact in producers and not fact.startswith(('gripper:', 'held:')):
                edges.append({'from': producers[fact], 'to': step_id, 'kind': 'state_fact', 'fact': fact})
        for fact in nodes[-1]['consumes']: producers.pop(fact, None)
        for fact in nodes[-1]['produces']: producers[fact] = step_id
        if action not in PAIR_KINDS:
            continue
        key = component_key(step)
        if PAIR_KINDS[action][1] == 'acquire':
            if held is not None:
                raise ValueError('上一个组件尚未释放，不能夹取另一组件：' + step_id)
            held = (key, index)
        else:
            if held is None or held[0] != key:
                raise ValueError('安装/连接缺少匹配的夹取前置步骤：' + step_id)
            producer = held[1]
            pairs[producer] = index
            pairs[index] = producer
            edges.append({'from': plan[producer]['step_id'], 'to': step_id,
                          'kind': 'held_component', 'fact': facts_for(step)['requires'][0]})
            held = None
    if held is not None:
        raise ValueError('协议末尾仍持有组件，缺少已登记的释放步骤')
    return {'nodes': nodes, 'edges': edges, 'pairs': pairs}
