# AgentCore Project

This project was created with the [AgentCore CLI](https://github.com/aws/agentcore-cli).

---

## AgentCore Gateway + Knowledge Base 連携

このエージェント (`BedrockAgent`) は、マニュアルに関する質問を **AgentCore Gateway 経由の
Knowledge Base** に問い合わせて回答する。

```text
GenU
  ↓  InvokeAgentRuntime
AgentCore Runtime (このエージェント / Strands)
  ↓  MCP over streamable HTTP + IAM (SigV4)
AgentCore Gateway
  ↓  Knowledge Base Connector
Managed Knowledge Base (sample-manual)
```

### 構成リソース (ap-northeast-1)

| リソース | 値 |
| --- | --- |
| Gateway | `sample-manual-gw-s08lru8q3m` |
| Gateway URL | `https://sample-manual-gw-s08lru8q3m.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp` |
| Gateway 認証 | `AWS_IAM` (SigV4) |
| Knowledge Base | `sample-manual-kb` (`UFPZW5A69W`) |

Gateway と Knowledge Base は AgentCore CLI プロジェクトの外で作成済みのため、
`agentcore/agentcore.json` の `agentCoreGateways` / `knowledgeBases` は空のままでよい。
接続先は下記の環境変数だけで決まる。

### 必要ライブラリ

| ライブラリ | 用途 |
| --- | --- |
| `mcp-proxy-for-aws` | Gateway を SigV4 署名付きで呼ぶ MCP トランスポート (`aws_iam_streamablehttp_client`) |
| `strands-agents` | Agent 本体。`MCPClient` は `ToolProvider` なので Gateway のツールを自動認識する |
| `bedrock-agentcore` | Runtime のエントリポイント (`BedrockAgentCoreApp`) |

依存は `app/BedrockAgent/pyproject.toml` に定義済み。

### 環境変数

| 変数 | 必須 | 説明 |
| --- | --- | --- |
| `AGENTCORE_GATEWAY_URL` | 必須 | Gateway の MCP エンドポイント URL。未設定なら Gateway ツール無しで起動する (KB は使えない) |
| `AWS_REGION` | 必須 | SigV4 署名に使うリージョン (`ap-northeast-1`)。Runtime では自動注入される |
| `AWS_PROFILE` | ローカルのみ | ローカル実行時の認証プロファイル。Runtime では未設定にし、実行ロールの認証情報を使う |

デプロイ時の `AGENTCORE_GATEWAY_URL` は `agentcore/agentcore.json` の
`runtimes[].envVars` で Runtime に渡している。ローカル実行時は自分で export する。

### Runtime 実行ロールに必要な IAM 権限

Gateway は IAM 認証のため、Runtime の実行ロールに Gateway 呼び出し権限が必要。
これが無いと `AccessDeniedException` になる。

この権限は手で書かず、`agentcore/agentcore.json` の `runtimes[].connections` で宣言する。
AgentCore CDK が実行ロールへ IAM 権限を自動生成する。

```json
"connections": [
  {
    "id": "sample-manual-gateway",
    "to": {
      "type": "gateway",
      "arn": "arn:aws:bedrock-agentcore:ap-northeast-1:035351467732:gateway/sample-manual-gw-s08lru8q3m",
      "outboundAuth": { "awsIam": {} }
    }
  }
]
```

`outboundAuth: { awsIam: {} }` は「実行ロールの認証情報で SigV4 署名して Gateway を呼ぶ」
という意味で、Gateway 側の inbound 認証 (`AWS_IAM`) と対になる。

生成される権限は実質的に次と同等。

```json
{
  "Effect": "Allow",
  "Action": "bedrock-agentcore:InvokeGateway",
  "Resource": "arn:aws:bedrock-agentcore:ap-northeast-1:035351467732:gateway/sample-manual-gw-s08lru8q3m"
}
```

Knowledge Base の検索は Gateway のサービスロールが実行するため、Runtime ロール側に
`bedrock:Retrieve` は不要。この他に既存の `bedrock:InvokeModel` /
`bedrock:InvokeModelWithResponseStream` (モデル呼び出し用) が必要。

### ローカル起動

```bash
cd BedrockAgent
export AWS_REGION=ap-northeast-1
export AWS_PROFILE=rag-poc-admin
export AGENTCORE_GATEWAY_URL="https://sample-manual-gw-s08lru8q3m.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"

agentcore dev
```

起動ログに Gateway から取得したツール一覧が出る。ここに Knowledge Base のツールが
出ていれば接続成功。

```text
INFO  Gateway tools (1): <ツール名>
```

### デプロイ

`agentcore` CLI は SSO プロファイルを解決しないため、一時認証情報を環境変数へ展開してから実行する。

```bash
cd BedrockAgent
eval "$(aws configure export-credentials --profile rag-poc-admin --format env)"
agentcore deploy --target dev -y -v
```

Gateway の URL と IAM 権限は `agentcore/agentcore.json` (`envVars` / `connections`) で
設定済みのため、デプロイ時に追加の手当ては不要。Gateway を差し替えたときは、この2箇所の
URL と ARN を更新する。

### 動作確認

```text
Q: あなたの名前は？        → 別戸六区 英慈円斗です。            (システムプロンプト)
Q: システム利用時間は？    → 平日8:00〜18:00です。               (Knowledge Base)
Q: 担当者は？              → 別戸六区 英慈円斗です。            (Knowledge Base)
```

マニュアルに無い内容を聞いた場合は、推測せず「マニュアルに記載が無い」と回答する。

うまく動かないときは CloudWatch Logs の
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` を確認する。

- `AccessDeniedException` → Runtime ロールの `bedrock-agentcore:InvokeGateway` を確認
- `Gateway tools (0)` / ツール一覧が空 → Gateway のターゲット (KB Connector) の状態を確認
- `AGENTCORE_GATEWAY_URL is not set` → Runtime の環境変数を確認

---

## Project Structure

```
my-project/
├── AGENTS.md               # AI coding assistant context
├── agentcore/
│   ├── agentcore.json      # Project config (agents, memories, credentials, gateways, evaluators)
│   ├── aws-targets.json    # Deployment targets (account + region)
│   ├── .env.local          # Secrets — API keys (gitignored)
│   ├── .llm-context/       # TypeScript type definitions for AI assistants
│   │   ├── agentcore.ts    # AgentCoreProjectSpec types
│   │   ├── aws-targets.ts  # Deployment target types
│   │   └── mcp.ts          # Gateway and MCP tool types
│   └── cdk/                # CDK infrastructure (@aws/agentcore-cdk)
├── app/                    # Agent application code
└── evaluators/             # Custom evaluator code (if any)
```

## Getting Started

### Prerequisites

- **Node.js** 20.x or later
- **Python 3.10+** and **uv** for Python agents ([install uv](https://docs.astral.sh/uv/getting-started/installation/))
- **AWS credentials** configured (`aws configure` or environment variables)
- **Docker** (only for Container build agents)

### Development

Run your agent locally:

```bash
agentcore dev
```

### Deployment

Deploy to AWS:

```bash
agentcore deploy
```

## Commands

| Command | Description |
| --- | --- |
| `agentcore create` | Create a new AgentCore project |
| `agentcore add` | Add resources (agent, memory, credential, gateway, evaluator, policy) |
| `agentcore remove` | Remove resources |
| `agentcore dev` | Run agent locally with hot-reload |
| `agentcore deploy` | Deploy to AWS via CDK |
| `agentcore status` | Show deployment status |
| `agentcore invoke` | Invoke agent (local or deployed) |
| `agentcore logs` | View agent logs |
| `agentcore traces` | View agent traces |
| `agentcore eval` | Run evaluations |
| `agentcore package` | Package agent artifacts |
| `agentcore validate` | Validate configuration |
| `agentcore pause` | Pause a deployed agent |
| `agentcore resume` | Resume a paused agent |
| `agentcore fetch` | Fetch remote resource definitions |
| `agentcore import` | Import existing resources |
| `agentcore update` | Check for CLI updates |

## Configuration

Edit the JSON files in `agentcore/` to configure your project. See `agentcore/.llm-context/` for type definitions and validation constraints.

The project uses a **flat resource model** — agents, memories, credentials, gateways, evaluators, and policies are top-level arrays in `agentcore.json`. Resources are independent; agents discover memories and credentials at runtime via environment variables or SDK calls.

## Resources

| Resource | Purpose |
| --- | --- |
| Agent (runtime) | HTTP, MCP, or A2A agent deployed to AgentCore Runtime |
| Memory | Persistent context storage with configurable strategies |
| Credential | API key or OAuth credential providers |
| Gateway | MCP gateway that routes tool calls to targets |
| Gateway Target | Tool implementation (Lambda, MCP server, OpenAPI, Smithy, API Gateway) |
| Evaluator | Custom LLM-as-a-Judge or code-based evaluation |
| Online Eval Config | Continuous evaluation pipeline for deployed agents |
| Policy | Cedar authorization policies for gateway tools |

### Agent Types

- **Template agents**: Created from framework templates (Strands, LangChain/LangGraph, GoogleADK, OpenAI Agents, Autogen)
- **BYO agents**: Bring your own code with `agentcore add agent --type byo`
- **Import agents**: Import existing Bedrock agents with `agentcore import`

### Build Types

- **CodeZip**: Python source packaged as a zip and deployed directly to AgentCore Runtime
- **Container**: Docker image built via CodeBuild (ARM64), pushed to ECR, and deployed to AgentCore Runtime

## Documentation

- [AgentCore CLI](https://github.com/aws/agentcore-cli)
- [AgentCore CDK Constructs](https://github.com/aws/agentcore-l3-cdk-constructs)
- [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)
