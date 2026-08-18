import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
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
import { ensureJsonSecret, ensureOriginVerifySecret } from './ensure-secret';

const STYLES_BUCKET_NAME = 'autocaption-styles-deadzone-423623826655';
const BACKEND_API_KEY_SECRET = 'autocaption/backend-api-key';
const GROQ_API_KEY_SECRET = 'autocaption/groq-api-key';
const ORIGIN_VERIFY_SECRET = 'autocaption/cf-origin-secret';
const ORIGIN_VERIFY_HEADER = 'X-Autocaption-Origin';

function grantSecretReadByName(grantee: iam.IGrantable, stack: cdk.Stack, secretNames: string[]): void {
  // fromSecretNameV2 grantRead uses a partial ARN. IAM needs the
  // Secrets-Manager suffix; a name* resource matches name-<6 chars>.
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
      resources: secretNames.map(
        (name) =>
          `arn:${cdk.Aws.PARTITION}:secretsmanager:${stack.region}:${stack.account}:secret:${name}*`,
      ),
    }),
  );
}

export class AutocaptionStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const stylesBucketName = STYLES_BUCKET_NAME;

    ensureJsonSecret(this, 'EnsureBackendApiKey', BACKEND_API_KEY_SECRET, {
      apiKey: 'REPLACE_ME',
    }, 'random');
    ensureJsonSecret(this, 'EnsureGroqApiKey', GROQ_API_KEY_SECRET, {
      apiKey: 'REPLACE_ME',
    });
    const originVerifyToken = ensureOriginVerifySecret(
      this,
      'EnsureOriginVerifySecret',
      ORIGIN_VERIFY_SECRET,
    );

    const apiKeySecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'BackendApiKeyRef',
      BACKEND_API_KEY_SECRET,
    );
    const groqSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'GroqApiKeyRef',
      GROQ_API_KEY_SECRET,
    );

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

    // Keep the original 4-subnet /18 layout. The live VPC was created with CDK
    // defaults (2 AZs, public+private, no cidrMask) which evenly split 10.0.0.0/16:
    //   Public  10.0.0.0/18, 10.0.64.0/18
    //   Private 10.0.128.0/18, 10.0.192.0/18
    // Switching to public-only + cidrMask 24 tries to replace those subnets with
    // 10.0.0.0/24 and 10.0.1.0/24, which overlap the existing /18s and fail CFN
    // (create-before-destroy). Isolated private subnets stay unused; NAT is removed.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
        },
      ],
    });

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsights: true });

    const backendImage = new ecr_assets.DockerImageAsset(this, 'BackendImage', {
      directory: path.join(__dirname, '../../..'),
      file: 'backend/Dockerfile',
      platform: ecr_assets.Platform.LINUX_AMD64,
    });

    const workerTaskRole = new iam.Role(this, 'WorkerTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    jobsTable.grantReadWriteData(workerTaskRole);
    mediaBucket.grantReadWrite(workerTaskRole);
    workerTaskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
        resources: [
          `arn:aws:s3:::${stylesBucketName}`,
          `arn:aws:s3:::${stylesBucketName}/*`,
        ],
      }),
    );

    const workerExecRole = new iam.Role(this, 'WorkerExecRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    workerExecRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
    );
    grantSecretReadByName(workerExecRole, this, [BACKEND_API_KEY_SECRET, GROQ_API_KEY_SECRET]);

    const logGroup = new logs.LogGroup(this, 'ApiLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const workerEnv = {
      AWS_REGION: cdk.Stack.of(this).region,
      AWS_DEFAULT_REGION: cdk.Stack.of(this).region,
      STYLE_STORAGE: 's3',
      AWS_S3_BUCKET: stylesBucketName,
      MEDIA_BUCKET: mediaBucket.bucketName,
      JOBS_TABLE_NAME: jobsTable.tableName,
      WHISPERX_DEVICE: 'cpu',
      KARAOKE_ENABLED_DEFAULT: 'true',
    };
    const workerSecrets = {
      BACKEND_API_KEY: ecs.Secret.fromSecretsManager(apiKeySecret, 'apiKey'),
      GROQ_API_KEY: ecs.Secret.fromSecretsManager(groqSecret, 'apiKey'),
    };

    const transcribeTask = new ecs.FargateTaskDefinition(this, 'TranscribeTask', {
      memoryLimitMiB: 1024,
      cpu: 512,
      taskRole: workerTaskRole,
      executionRole: workerExecRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    transcribeTask.addContainer('worker', {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'transcribe', logGroup }),
      environment: workerEnv,
      secrets: workerSecrets,
      command: [
        'python',
        '-c',
        'raise SystemExit("start via RunTask worker override")',
      ],
    });

    const burnTask = new ecs.FargateTaskDefinition(this, 'BurnTask', {
      memoryLimitMiB: 8192,
      cpu: 4096,
      taskRole: workerTaskRole,
      executionRole: workerExecRole,
      ephemeralStorageGiB: 40,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    burnTask.addContainer('worker', {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'burn', logGroup }),
      environment: workerEnv,
      secrets: workerSecrets,
      command: [
        'python',
        '-c',
        'raise SystemExit("start via RunTask worker override")',
      ],
    });

    const workerSg = new ec2.SecurityGroup(this, 'WorkerSg', {
      vpc,
      allowAllOutbound: true,
      description: 'One-shot Autocaption Fargate workers (egress only)',
    });

    const controlFn = new lambda.Function(this, 'ControlPlane', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.seconds(29),
      memorySize: 512,
      environment: {
        JOBS_TABLE_NAME: jobsTable.tableName,
        MEDIA_BUCKET: mediaBucket.bucketName,
        STYLES_BUCKET: stylesBucketName,
        API_KEY_SECRET_NAME: BACKEND_API_KEY_SECRET,
        ECS_CLUSTER: cluster.clusterName,
        TRANSCRIBE_TASK_FAMILY: transcribeTask.family,
        BURN_TASK_FAMILY: burnTask.family,
        CONTAINER_NAME: 'worker',
        ECS_SUBNETS: vpc.publicSubnets.map((s) => s.subnetId).join(','),
        ECS_SECURITY_GROUP: workerSg.securityGroupId,
        KARAOKE_ENABLED_DEFAULT: 'true',
        ORIGIN_SECRET: originVerifyToken,
      },
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/control_plane')),
    });
    jobsTable.grantReadWriteData(controlFn);
    mediaBucket.grantReadWrite(controlFn);
    grantSecretReadByName(controlFn, this, [BACKEND_API_KEY_SECRET]);
    controlFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
        resources: [
          `arn:aws:s3:::${stylesBucketName}`,
          `arn:aws:s3:::${stylesBucketName}/*`,
        ],
      }),
    );
    controlFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['ecs:RunTask'],
        resources: [
          cluster.clusterArn,
          `arn:aws:ecs:${this.region}:${this.account}:task-definition/${transcribeTask.family}*`,
          `arn:aws:ecs:${this.region}:${this.account}:task-definition/${burnTask.family}*`,
        ],
      }),
    );
    controlFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['iam:PassRole'],
        resources: [workerTaskRole.roleArn, workerExecRole.roleArn],
      }),
    );

    // NONE: browser POSTs cannot satisfy Function URL IAM/OAC payload signing.
    // App auth stays X-API-Key. CloudFront injects ORIGIN_SECRET so the raw
    // Function URL is not a public backdoor. CDK adds InvokeFunctionUrl +
    // InvokeFunction (required for Function URLs created after Oct 2025).
    const fnUrl = controlFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
    });

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

    // SPA deep links have no file extension. Rewrite them to index.html on the
    // default (S3) behavior only — never on /api*. Do not use distribution-wide
    // 403→index.html: Function URL IAM 403s would become HTML 200 and hide API errors.
    const spaFallback = new cloudfront.Function(this, 'SpaFallback', {
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var request = event.request;
  if (request.uri.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
`),
    });

    const apiOriginRequestPolicy = new cloudfront.OriginRequestPolicy(this, 'ApiOriginRequestPolicy', {
      comment: 'Forward API/CORS headers; Host stays the Function URL domain',
      headerBehavior: cloudfront.OriginRequestHeaderBehavior.allowList(
        'Origin',
        'Access-Control-Request-Headers',
        'Access-Control-Request-Method',
        'Content-Type',
        'X-API-Key',
      ),
      queryStringBehavior: cloudfront.OriginRequestQueryStringBehavior.all(),
      cookieBehavior: cloudfront.OriginRequestCookieBehavior.none(),
    });

    const distribution = new cloudfront.Distribution(this, 'UiCdn', {
      defaultBehavior: {
        origin: new origins.S3Origin(uiBucket, { originAccessIdentity: oai }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        functionAssociations: [
          {
            function: spaFallback,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
        ],
      },
      additionalBehaviors: {
        '/api*': {
          origin: new origins.FunctionUrlOrigin(fnUrl, {
            readTimeout: cdk.Duration.seconds(60),
            customHeaders: {
              [ORIGIN_VERIFY_HEADER]: originVerifyToken,
            },
          }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: apiOriginRequestPolicy,
          functionAssociations: [
            {
              function: apiPathRewrite,
              eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
            },
          ],
        },
      },
      defaultRootObject: 'index.html',
    });

    const apiBaseUrl = `https://${distribution.distributionDomainName}/api`;
    const uiBaseUrl = `https://${distribution.distributionDomainName}`;

    const uiOut = path.join(__dirname, '..', '..', '..', 'out');
    if (fs.existsSync(uiOut)) {
      new s3deploy.BucketDeployment(this, 'DeployUi', {
        sources: [s3deploy.Source.asset(uiOut)],
        destinationBucket: uiBucket,
        distribution,
        distributionPaths: ['/*'],
      });
    }

    new cdk.CfnOutput(this, 'ApiBaseUrl', { value: apiBaseUrl });
    new cdk.CfnOutput(this, 'CloudFrontUrl', { value: uiBaseUrl });
    new cdk.CfnOutput(this, 'BackendImageUri', { value: backendImage.imageUri });
    new cdk.CfnOutput(this, 'MediaBucketName', { value: mediaBucket.bucketName });
    new cdk.CfnOutput(this, 'UiBucketName', { value: uiBucket.bucketName });
    new cdk.CfnOutput(this, 'JobsTableName', { value: jobsTable.tableName });
    new cdk.CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'TranscribeTaskFamily', { value: transcribeTask.family });
    new cdk.CfnOutput(this, 'BurnTaskFamily', { value: burnTask.family });
  }
}
