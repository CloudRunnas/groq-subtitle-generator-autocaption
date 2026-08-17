import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

/**
 * Create a Secrets Manager secret only when it does not already exist.
 * Never overwrites an existing value (live Groq / API keys stay untouched).
 */
export function ensureJsonSecret(
  scope: Construct,
  id: string,
  secretName: string,
  initialJson: Record<string, string>,
  kind: 'literal' | 'random' = 'literal',
): void {
  const fn = new lambda.Function(scope, `${id}Fn`, {
    runtime: lambda.Runtime.PYTHON_3_12,
    handler: 'index.handler',
    timeout: cdk.Duration.seconds(30),
    memorySize: 128,
    code: lambda.Code.fromInline(`
import json
import secrets
import boto3

sm = boto3.client("secretsmanager")

def handler(event, context):
    props = event.get("ResourceProperties") or {}
    name = props["SecretName"]
    initial = props.get("InitialJson") or "{}"
    kind = props.get("Kind") or "literal"
    request_type = event.get("RequestType")
    if request_type == "Delete":
        return {"PhysicalResourceId": name}
    try:
        sm.describe_secret(SecretId=name)
    except sm.exceptions.ResourceNotFoundException:
        if kind == "random":
            initial = json.dumps({"apiKey": secrets.token_urlsafe(48)})
        sm.create_secret(Name=name, SecretString=initial)
    return {"PhysicalResourceId": name}
`),
  });
  fn.addToRolePolicy(
    new iam.PolicyStatement({
      actions: ['secretsmanager:CreateSecret', 'secretsmanager:DescribeSecret'],
      resources: ['*'],
    }),
  );

  const provider = new cr.Provider(scope, `${id}Provider`, { onEventHandler: fn });
  new cdk.CustomResource(scope, id, {
    serviceToken: provider.serviceToken,
    properties: {
      SecretName: secretName,
      InitialJson: JSON.stringify(initialJson),
      Kind: kind,
    },
  });
}
