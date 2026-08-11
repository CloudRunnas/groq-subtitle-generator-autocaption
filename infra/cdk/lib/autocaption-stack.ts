import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as path from 'path';
import * as fs from 'fs';

export class AutocaptionStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const stylesBucketName =
      this.node.tryGetContext('stylesBucketName') ||
      process.env.AWS_S3_BUCKET ||
      'autocaption-styles-deadzone-423623826655';

    const backendApiKey =
      process.env.BACKEND_API_KEY ||
      // fallback: CDK generates if env not set at deploy time
      '';
    const apiKeySecret = backendApiKey
      ? new secretsmanager.Secret(this, 'BackendApiKey', {
          secretName: 'autocaption/backend-api-key',
          secretStringValue: cdk.SecretValue.unsafePlainText(
            JSON.stringify({ apiKey: backendApiKey }),
          ),
        })
      : new secretsmanager.Secret(this, 'BackendApiKey', {
          secretName: 'autocaption/backend-api-key',
          generateSecretString: {
            secretStringTemplate: JSON.stringify({}),
            generateStringKey: 'apiKey',
            excludePunctuation: true,
            passwordLength: 64,
          },
        });

    const groqApiKey = process.env.GROQ_API_KEY || 'REPLACE_ME';
    const groqSecret = new secretsmanager.Secret(this, 'GroqApiKey', {
      secretName: 'autocaption/groq-api-key',
      description: 'GROQ_API_KEY for transcription/translation',
      secretStringValue: cdk.SecretValue.unsafePlainText(
        JSON.stringify({ apiKey: groqApiKey }),
      ),
    });

    const jobsTable = new dynamodb.Table(this, 'AutocaptionJobs', {
      tableName: 'AutocaptionJobs',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    jobsTable.addGlobalSecondaryIndex({
      indexName: 'StatusByCreated',
      partitionKey: { name: 'statusKey', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdSk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['jobId', 'status', 'filename', 'progress', 'mode', 'createdAt', 'updatedAt'],
    });
    jobsTable.addGlobalSecondaryIndex({
      indexName: 'TenantByCreated',
      partitionKey: { name: 'tenantId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdSk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['jobId', 'status', 'filename', 'progress', 'mode', 'createdAt', 'updatedAt'],
    });

    const mediaBucket = new s3.Bucket(this, 'MediaBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.HEAD],
          allowedOrigins: ['*'],
          allowedHeaders: ['*'],
          exposedHeaders: ['ETag'],
        },
      ],
      lifecycleRules: [{ expiration: cdk.Duration.days(14), prefix: 'jobs/' }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const uiBucket = new s3.Bucket(this, 'UiBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const oai = new cloudfront.OriginAccessIdentity(this, 'UiOAI');
    uiBucket.grantRead(oai);

    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1,
    });

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsights: true });

    const backendImage = new ecr_assets.DockerImageAsset(this, 'BackendImage', {
      directory: path.join(__dirname, '../../..'),
      file: 'backend/Dockerfile',
      platform: ecr_assets.Platform.LINUX_AMD64,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'ApiTask', {
      memoryLimitMiB: 8192,
      cpu: 4096,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'ApiLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const container = taskDef.addContainer('api', {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'api', logGroup }),
      environment: {
        AWS_REGION: cdk.Stack.of(this).region,
        AWS_DEFAULT_REGION: cdk.Stack.of(this).region,
        STYLE_STORAGE: 's3',
        AWS_S3_BUCKET: stylesBucketName,
        MEDIA_BUCKET: mediaBucket.bucketName,
        JOBS_TABLE_NAME: jobsTable.tableName,
        WHISPERX_DEVICE: 'cpu',
        KARAOKE_ENABLED_DEFAULT: 'true',
      },
      secrets: {
        BACKEND_API_KEY: ecs.Secret.fromSecretsManager(apiKeySecret, 'apiKey'),
        GROQ_API_KEY: ecs.Secret.fromSecretsManager(groqSecret, 'apiKey'),
      },
      healthCheck: {
        command: [
          'CMD-SHELL',
          'python -c "import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:8000/health\')" || exit 1',
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(180),
      },
    });
    container.addPortMappings({ containerPort: 8000 });

    jobsTable.grantReadWriteData(taskDef.taskRole);
    mediaBucket.grantReadWrite(taskDef.taskRole);
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
        resources: [
          `arn:aws:s3:::${stylesBucketName}`,
          `arn:aws:s3:::${stylesBucketName}/*`,
        ],
      }),
    );
    apiKeySecret.grantRead(taskDef.taskRole);
    groqSecret.grantRead(taskDef.taskRole);

    const service = new ecs.FargateService(this, 'ApiService', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      circuitBreaker: { rollback: true },
      enableExecuteCommand: true,
      minHealthyPercent: 50,
      maxHealthyPercent: 200,
    });

    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
    });
    const listener = alb.addListener('Http', { port: 80, open: true });
    listener.addTargets('ApiTarget', {
      port: 8000,
      targets: [service],
      healthCheck: {
        path: '/health',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 5,
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // Strip /api prefix so UI and API share one HTTPS CloudFront origin (no mixed content / CORS).
    const apiPathRewrite = new cloudfront.Function(this, 'ApiPathRewrite', {
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri === '/api' || uri.indexOf('/api/') === 0) {
    var next = uri.slice(4);
    request.uri = next.length > 0 ? next : '/';
  }
  return request;
}
`),
    });

    const albOrigin = new origins.LoadBalancerV2Origin(alb, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80,
      // CloudFront origin timeouts are capped at 60s without a quota increase.
      readTimeout: cdk.Duration.seconds(60),
      keepaliveTimeout: cdk.Duration.seconds(60),
    });

    const distribution = new cloudfront.Distribution(this, 'UiCdn', {
      defaultBehavior: {
        origin: new origins.S3Origin(uiBucket, { originAccessIdentity: oai }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      additionalBehaviors: {
        '/api*': {
          origin: albOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          functionAssociations: [
            {
              function: apiPathRewrite,
              eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
            },
          ],
        },
      },
      defaultRootObject: 'index.html',
      // Only remap S3-style access denials for the SPA; do not rewrite API status codes.
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
      ],
    });

    const apiBaseUrl = `https://${distribution.distributionDomainName}/api`;
    const uiBaseUrl = `https://${distribution.distributionDomainName}`;

    container.addEnvironment(
      'CORS_ORIGINS',
      `${uiBaseUrl},http://localhost:3000,http://127.0.0.1:3000`,
    );

    const warmupFn = new lambda.Function(this, 'WarmupLambda', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      environment: {
        BACKEND_URL: `http://${alb.loadBalancerDnsName}`,
      },
      code: lambda.Code.fromInline(`
import json, os, urllib.request

def handler(event, context):
    base = os.environ.get("BACKEND_URL", "").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/warmup", timeout=10) as r:
            body = r.read().decode()
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}, "body": body}
    except Exception as e:
        return {"statusCode": 502, "headers": {"Access-Control-Allow-Origin": "*"}, "body": json.dumps({"error": str(e)})}
`),
    });
    const warmupUrl = warmupFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.GET],
      },
    });

    const uiOut = path.join(__dirname, '..', '..', '..', 'out');
    if (fs.existsSync(uiOut)) {
      new s3deploy.BucketDeployment(this, 'DeployUi', {
        sources: [s3deploy.Source.asset(uiOut)],
        destinationBucket: uiBucket,
        distribution,
        distributionPaths: ['/*'],
      });
    }

    new cdk.CfnOutput(this, 'AlbDns', { value: alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'ApiBaseUrl', { value: apiBaseUrl });
    new cdk.CfnOutput(this, 'CloudFrontUrl', { value: uiBaseUrl });
    new cdk.CfnOutput(this, 'WarmupUrl', { value: warmupUrl.url });
    new cdk.CfnOutput(this, 'BackendImageUri', { value: backendImage.imageUri });
    new cdk.CfnOutput(this, 'MediaBucketName', { value: mediaBucket.bucketName });
    new cdk.CfnOutput(this, 'UiBucketName', { value: uiBucket.bucketName });
    new cdk.CfnOutput(this, 'JobsTableName', { value: jobsTable.tableName });
    new cdk.CfnOutput(this, 'ApiKeySecretArn', { value: apiKeySecret.secretArn });
  }
}
