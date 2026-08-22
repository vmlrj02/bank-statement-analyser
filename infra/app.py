#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.bsa_stack import BsaStack

# Asset bundling runs Docker and takes minutes, which is far too slow for a
# check that only needs the template — such as the dependency-cycle check in
# CI (scripts/check_template_cycles.py). CloudFormation rejects a cyclic
# template only when the changeset is created, so catching it needs a synth,
# and a synth should not need Docker to produce a graph.
_context = {}
if os.environ.get("CDK_SKIP_BUNDLING"):
    _context["aws:cdk:bundling-stacks"] = []

app = cdk.App(context=_context)
BsaStack(
    app, "BsaStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="Bank Statement Analyser — MVP (upload UI, extraction pipeline, outputs)",
)
app.synth()
