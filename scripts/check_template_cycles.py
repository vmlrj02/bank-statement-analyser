#!/usr/bin/env python3
"""Find dependency cycles in a synthesized CloudFormation template.

`cdk synth` happily produces a template CloudFormation will refuse, and you
only find out when the changeset is created — after Docker bundling and asset
publishing, minutes into a deploy. This reads the template and walks the same
graph CloudFormation does: Ref, Fn::GetAtt and DependsOn.

    cdk synth --quiet -c aws:cdk:bundling-stacks=[]     # fast, no Docker
    python scripts/check_template_cycles.py infra/cdk.out/BsaStack.template.json

It caught this for real: giving the processor Lambda an on-failure destination
pointing at the sweeper made the PROCESSOR's role reference the sweeper, while
the sweeper's env and invoke permission referenced the processor. The fix was
an SQS queue between them, which depends on neither.
"""
import json
import sys


def _refs(node, out):
    """Every logical id this node depends on."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "Ref" and isinstance(v, str):
                out.add(v)
            elif k == "Fn::GetAtt":
                if isinstance(v, list) and v and isinstance(v[0], str):
                    out.add(v[0])
                elif isinstance(v, str):
                    out.add(v.split(".")[0])
            elif k == "DependsOn":
                out.update([v] if isinstance(v, str) else v)
            else:
                _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)
    return out


def cycles(template: dict) -> list[list[str]]:
    resources = template.get("Resources", {})
    graph = {name: {r for r in _refs(body, set()) if r in resources and r != name}
             for name, body in resources.items()}

    found, colour, stack = [], {}, []

    def visit(n):
        colour[n] = 1                       # grey: on the current path
        stack.append(n)
        for m in sorted(graph[n]):
            if colour.get(m) == 1:
                found.append(stack[stack.index(m):] + [m])
            elif colour.get(m) is None:
                visit(m)
        stack.pop()
        colour[n] = 2                       # black: finished

    for n in sorted(graph):
        if colour.get(n) is None:
            visit(n)
    return found


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: check_template_cycles.py <template.json>")
    with open(sys.argv[1]) as fh:
        template = json.load(fh)
    bad = cycles(template)
    if not bad:
        print(f"ok — no dependency cycles in {len(template.get('Resources', {}))} "
              f"resources")
        return 0
    print(f"CIRCULAR DEPENDENCY — CloudFormation will refuse this template:")
    for cyc in bad:
        print("  " + " -> ".join(cyc))
    return 1


if __name__ == "__main__":
    sys.exit(main())
