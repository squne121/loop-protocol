# Named SubAgent resume 互換性説明

この説明は `--named-subagent-resume` scenario の invocation にだけ、CLI の `--append-system-prompt-file` で追加される互換性説明です。恒久的な system prompt ではなく、通常の session 起動には注入しません。

- `Agent` tool は任意引数 `name` を受け付けます。固定試験 prompt が `name` を指定している場合は、その値を省略せず、そのまま `Agent` tool の `name` に渡してください。
- 同じ session で name 付きの `Agent` を呼んだあと、`SendMessage` の `to` にその name を渡すと、新しい `Agent` 呼び出しなしに、同じ agent ID の SubAgent が resume されます。
- `SendMessage` の `to` には、`Agent` tool に渡した name か、実際に返された agent ID だけを指定します。`subagent_type`（agent の種類名）や推測した名前は宛先に使いません。
- `SendMessage` が tool 一覧に見えない場合は、ToolSearch で `select:SendMessage` を使って読み込んでから呼び出します。
