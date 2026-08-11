"""Check one rendered chart against the repo's availability conventions.

Reads a rendered manifest stream on stdin, takes the chart's path as argv[1],
prints one line per problem, and exits non-zero if there were any. Driven by
scripts/ci-lint-availability.sh, which explains why this inspects the rendered
output rather than the templates.
"""

import sys

import yaml


def pod_annotations(spec):
    return spec["template"]["metadata"].get("annotations") or {}


def check(chart, docs):
    problems = []

    def selector_of(obj):
        return tuple(sorted((obj["spec"].get("selector", {}).get("matchLabels") or {}).items()))

    pdb_selectors = [selector_of(d) for d in docs if d.get("kind") == "PodDisruptionBudget"]

    for d in docs:
        if d.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        name = d["metadata"]["name"]
        spec = d["spec"]
        # An absent `replicas` means 1, which is exactly the case the
        # "no PDB on a singleton" rule exists to catch, so default it rather
        # than skipping.
        replicas = spec.get("replicas", 1)
        pod = spec["template"]["spec"]
        has_pdb = selector_of(d) in pdb_selectors

        if replicas >= 2:
            if not has_pdb:
                problems.append(f"{name}: replicas={replicas} but no PodDisruptionBudget")
            if not pod.get("topologySpreadConstraints"):
                problems.append(f"{name}: replicas={replicas} but no topologySpreadConstraints")
        elif has_pdb:
            problems.append(
                f"{name}: replicas={replicas} WITH a PodDisruptionBudget "
                "- this blocks kubectl drain permanently"
            )

        grace = pod.get("terminationGracePeriodSeconds")
        longest_sleep = 0
        # Opt-out for pods that carry a readinessProbe but genuinely have
        # nothing routing to them through a Service — an outbound-only
        # connector probes itself to report health, and a preStop sleep there
        # delays the shutdown without protecting anything. The annotation's
        # value is the reason, and it is required to be non-empty so the
        # exemption has to be justified in the chart rather than just claimed.
        exempt = (pod_annotations(spec) or {}).get("availability.zachd/prestop-exempt", "").strip()
        for c in pod.get("containers", []):
            pre = (c.get("lifecycle") or {}).get("preStop")
            # A readinessProbe is the usable proxy for "something routes to
            # this": a container nothing reaches has no reason to carry one.
            if c.get("readinessProbe") and not pre and not exempt:
                problems.append(f"{name}/{c['name']}: has a readinessProbe but no preStop hook")
            if pre and "sleep" in pre:
                longest_sleep = max(longest_sleep, int(pre["sleep"].get("seconds", 0)))

        if longest_sleep:
            if grace is None:
                problems.append(
                    f"{name}: preStop sleep {longest_sleep}s but no explicit "
                    "terminationGracePeriodSeconds"
                )
            elif grace <= longest_sleep:
                problems.append(
                    f"{name}: terminationGracePeriodSeconds={grace} does not exceed "
                    f"preStop sleep {longest_sleep}s"
                )

    return problems


def main():
    chart = sys.argv[1]
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    problems = check(chart, docs)
    for p in problems:
        print(f"  {chart}: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
