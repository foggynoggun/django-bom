"""Render a part-class workflow as an indented text tree.

Ported from production bfc72fa with two corrections, noted inline. The
commented-out pygraphviz/DotExporter image path from production is not carried
over: it was dead (never called, `using_graphviz` always resolved through an
ImportError guard) and pygraphviz is not a dependency on either side.
"""

from anytree import Node, RenderTree


def workflow_rows(initial_state, forward_transitions):
    """Return the workflow as [{'line': rendered, 'name': state name}, ...].

    The state name is carried alongside the rendered line so callers can mark the current state
    by equality. The template used to test `current_state.name in line`, which highlighted the
    wrong node whenever one state's name was a substring of another's ("Review" vs "Review
    Complete").
    """
    root = workflow_to_tree(initial_state, forward_transitions)
    return [{'line': f'{pre}{node.name}', 'name': node.name} for pre, _fill, node in RenderTree(root)]


def workflow_str(initial_state, forward_transitions):
    """Return the workflow as a list of pre-rendered tree lines."""
    return [row['line'] for row in workflow_rows(initial_state, forward_transitions)]


def workflow_to_tree(initial_state, forward_transitions):
    edges = {}
    for transition in forward_transitions:
        # CORRECTION 1: production keyed this dict on str(state), which for a final state is
        # "Name[final]" via PartClassWorkflowState.__str__. The root node was built from
        # initial_state.name, so the marker leaked into the rendered diagram and the two
        # namespaces did not line up. Key and label on .name throughout.
        source = transition.source_state.name
        target = transition.target_state.name

        edges.setdefault(source, []).append(target)

    root = Node(name=initial_state.name, children=[])
    _expand(root, edges, seen=frozenset())
    return root


def _expand(cur_node, edges, seen):
    """Attach children of cur_node, refusing to walk back into an ancestor.

    CORRECTION 2: production's helper() had no cycle guard. Transitions are editable
    through the Django admin, where nothing stops two states pointing at each other with
    direction_in_workflow='forward'. One such pair sent this into unbounded recursion,
    and this runs on every part-info page render for a part on that workflow.
    """
    if cur_node is None or cur_node.name in seen:
        return

    seen = seen | {cur_node.name}

    for target_name in edges.get(cur_node.name, []):
        if target_name in seen:
            # Cycle: show the edge, do not follow it.
            Node(name=f"{target_name} (loops back)", parent=cur_node)
            continue
        child_node = Node(name=target_name, parent=cur_node)
        _expand(child_node, edges, seen)
