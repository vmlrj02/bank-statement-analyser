#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.bsa_stack import BsaStack

app = cdk.App()
BsaStack(
    app, "BsaStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="Bank Statement Analyser — MVP (upload UI, extraction pipeline, outputs)",
)
app.synth()
