import { core } from "../lib/api";
import { useQuery } from "../lib/app";
import { QueryView } from "./Status";

type FileDiff = {
  readonly path: string;
  readonly lines: ReadonlyArray<string>;
  readonly added: number;
  readonly removed: number;
};

const OPEN_FILES = 8;

/** Path from a `diff --git a/x b/y` header; renames show the new path. */
function headerPath(header: string): string {
  const match = /^diff --git a\/(.*) b\/(.*)$/.exec(header);
  return match?.[2] ?? header.replace(/^diff --git /, "");
}

/** Split a unified diff into per-file sections; text before the first header is kept as-is. */
function splitFiles(content: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: { path: string; lines: string[]; added: number; removed: number } | null = null;
  for (const line of content.split("\n")) {
    if (line.startsWith("diff --git ")) {
      current = { path: headerPath(line), lines: [], added: 0, removed: 0 };
      files.push(current);
      continue;
    }
    if (current === null) {
      current = { path: "", lines: [], added: 0, removed: 0 };
      files.push(current);
    }
    current.lines.push(line);
    if (line.startsWith("+") && !line.startsWith("+++")) current.added += 1;
    else if (line.startsWith("-") && !line.startsWith("---")) current.removed += 1;
  }
  return files.filter((file) => file.path !== "" || file.lines.some((line) => line.trim() !== ""));
}

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "diff-line hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-line meta-line";
  if (line.startsWith("+")) return "diff-line add";
  if (line.startsWith("-")) return "diff-line del";
  return "diff-line";
}

/** Read-only view of an integration's retained patch, grouped by file. */
export function DiffView({
  workspaceId,
  runId,
  integrationId,
}: {
  workspaceId: string;
  runId: string;
  integrationId: string;
}) {
  const diff = useQuery(`integration-diff:${workspaceId}:${runId}:${integrationId}`, () =>
    core(workspaceId)
      .getIntegrationDiff(runId, integrationId)
      .then((r) => r.data),
  );

  return (
    <QueryView query={diff}>
      {(data) => {
        const files = splitFiles(data.content);
        const added = files.reduce((sum, file) => sum + file.added, 0);
        const removed = files.reduce((sum, file) => sum + file.removed, 0);
        return (
          <div className="diff">
            <div className="meta">
              {files.length} 个文件 · <span className="ok">+{added}</span> <span className="bad">−{removed}</span>
            </div>
            {data.truncated && (
              <div className="banner attn">
                <div className="banner-title">差异超过 1 MiB，只显示了开头部分</div>
                <div className="meta">完整补丁在整合工作区里，用 git diff 查看。</div>
              </div>
            )}
            {files.length === 0 && <div className="empty">这次整合没有代码改动。</div>}
            {files.map((file, index) => (
              <details key={`${index}:${file.path}`} className="diff-file" open={files.length <= OPEN_FILES}>
                <summary>
                  <span className="mono grow">{file.path || "补丁说明"}</span>
                  <span className="mono ok">+{file.added}</span>
                  <span className="mono bad">−{file.removed}</span>
                </summary>
                <div className="diff-body mono">
                  {file.lines
                    .filter((line) => !/^(index |new file mode|deleted file mode|similarity index)/.test(line))
                    .map((line, n) => (
                      <div key={n} className={lineClass(line)}>
                        {line || " "}
                      </div>
                    ))}
                </div>
              </details>
            ))}
          </div>
        );
      }}
    </QueryView>
  );
}
