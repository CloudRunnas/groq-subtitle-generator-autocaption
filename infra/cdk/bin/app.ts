#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AutocaptionStack } from '../lib/autocaption-stack';

const app = new cdk.App();
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT || process.env.AWS_ACCOUNT_ID,
  region: process.env.CDK_DEFAULT_REGION || process.env.AWS_REGION || 'eu-central-1',
};

new AutocaptionStack(app, 'AutocaptionStack', {
  env,
  description: 'Autocaption UI (S3/CloudFront) + Fargate API + Warmup Lambda + DynamoDB',
});
