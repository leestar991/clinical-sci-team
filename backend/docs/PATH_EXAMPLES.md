# 文件路径使用示例

## 三种路径类型

DeerFlow 的文件上传系统返回三种不同的路径，每种路径用于不同的场景：

### 1. 实际文件系统路径 (path)

```
.deer-flow/threads/{thread_id}/user-data/uploads/document.pdf
```

**用途：**
- 文件在服务器文件系统中的实际位置
- 相对于 `backend/` 目录
- 用于直接文件系统访问、备份、调试等

**示例：**
```python
# Python 代码中直接访问
from pathlib import Path

file_path = Path("backend/.deer-flow/threads/abc123/user-data/uploads/document.pdf")
content = file_path.read_bytes()
```

### 2. 虚拟路径 (virtual_path)

```
/mnt/user-data/uploads/document.pdf
```

**用途：**
- Agent 在沙箱环境中使用的路径
- 沙箱系统会自动映射到实际路径
- Agent 的所有文件操作工具都使用这个路径

**示例：**
Agent 在对话中使用：
```python
# Agent 使用 read_file 工具
read_file(path="/mnt/user-data/uploads/document.pdf")

# Agent 使用 bash 工具
bash(command="cat /mnt/user-data/uploads/document.pdf")
```

### 3. HTTP 访问 URL (artifact_url)

```
/api/threads/{thread_id}/artifacts/mnt/user-data/uploads/document.pdf
```

**用途：**
- 前端通过 HTTP 访问文件
- 用于下载、预览文件
- 可以直接在浏览器中打开

**示例：**
```typescript
// 前端 TypeScript/JavaScript 代码
const threadId = 'abc123';
const filename = 'document.pdf';

// 下载文件
const downloadUrl = `/api/threads/${threadId}/artifacts/mnt/user-data/uploads/${filename}?download=true`;
window.open(downloadUrl);

// 在新窗口预览
const viewUrl = `/api/threads/${threadId}/artifacts/mnt/user-data/uploads/${filename}`;
window.open(viewUrl, '_blank');

// 使用 fetch API 获取
const response = await fetch(viewUrl);
const blob = await response.blob();
```

## 完整使用流程示例

### 场景：前端上传文件并让 Agent 处理

```typescript
// 1. 前端上传文件
async function uploadAndProcess(threadId: string, file: File) {
  // 上传文件
  const formData = new FormData();
  formData.append('files', file);

  const uploadResponse = await fetch(
    `/api/threads/${threadId}/uploads`,
    {
      method: 'POST',
      body: formData
    }
  );

  const uploadData = await uploadResponse.json();
  const fileInfo = uploadData.files[0];

  console.log('文件信息：', fileInfo);
  // {
  //   filename: "report.pdf",
  //   path: ".deer-flow/threads/abc123/user-data/uploads/report.pdf",
  //   virtual_path: "/mnt/user-data/uploads/report.pdf",
  //   artifact_url: "/api/threads/abc123/artifacts/mnt/user-data/uploads/report.pdf",
  //   markdown_file: "report.md",
  //   markdown_path: ".deer-flow/threads/abc123/user-data/uploads/report.md",
  //   markdown_virtual_path: "/mnt/user-data/uploads/report.md",
  //   markdown_artifact_url: "/api/threads/abc123/artifacts/mnt/user-data/uploads/report.md"
  // }

  // 2. 发送消息给 Agent
  await sendMessage(threadId, "请分析刚上传的 PDF 文件");

  // Agent 会自动看到文件列表，包含：
  // - report.pdf (虚拟路径: /mnt/user-data/uploads/report.pdf)
  // - report.md (虚拟路径: /mnt/user-data/uploads/report.md)

  // 3. 前端可以直接访问转换后的 Markdown
  const mdResponse = await fetch(fileInfo.markdown_artifact_url);
  const markdownContent = await mdResponse.text();
  console.log('Markdown 内容：', markdownContent);

  // 4. 或者下载原始 PDF
  const downloadLink = document.createElement('a');
  downloadLink.href = fileInfo.artifact_url + '?download=true';
  downloadLink.download = fileInfo.filename;
  downloadLink.click();
}
```

## 路径转换表

| 场景 | 使用的路径类型 | 示例 |
|------|---------------|------|
| 服务器后端代码直接访问 | `path` | `.deer-flow/threads/abc123/user-data/uploads/file.pdf` |
| Agent 工具调用 | `virtual_path` | `/mnt/user-data/uploads/file.pdf` |
| 前端下载/预览 | `artifact_url` | `/api/threads/abc123/artifacts/mnt/user-data/uploads/file.pdf` |
| 备份脚本 | `path` | `.deer-flow/threads/abc123/user-data/uploads/file.pdf` |
| 日志记录 | `path` | `.deer-flow/threads/abc123/user-data/uploads/file.pdf` |

## 代码示例集合

### Python - 后端处理

```python
from pathlib import Path
from deerflow.agents.middlewares.thread_data_middleware import THREAD_DATA_BASE_DIR


def process_uploaded_file(thread_id: str, filename: str):
    # 使用实际路径
    base_dir = Path.cwd() / THREAD_DATA_BASE_DIR / thread_id / "user-data" / "uploads"
    file_path = base_dir / filename

    # 直接读取
    with open(file_path, "rb") as f:
        content = f.read()

    return content
```

### JavaScript - 前端访问

```javascript
// 列出已上传的文件
async function listUploadedFiles(threadId) {
  const response = await fetch(`/api/threads/${threadId}/uploads/list`);
  const data = await response.json();

  // 为每个文件创建下载链接
  data.files.forEach(file => {
    console.log(`文件: ${file.filename}`);
    console.log(`下载: ${file.artifact_url}?download=true`);
    console.log(`预览: ${file.artifact_url}`);

    // 如果是文档，还有 Markdown 版本
    if (file.markdown_artifact_url) {
      console.log(`Markdown: ${file.markdown_artifact_url}`);
    }
  });

  return data.files;
}

// 删除文件
async function deleteFile(threadId, filename) {
  const response = await fetch(
    `/api/threads/${threadId}/uploads/${filename}`,
    { method: 'DELETE' }
  );
  return response.json();
}
```

### React 组件示例

```tsx
import React, { useState, useEffect } from 'react';

interface UploadedFile {
  filename: string;
  size: number;
  path: string;
  virtual_path: string;
  artifact_url: string;
  extension: string;
  modified: number;
  markdown_artifact_url?: string;
}

function FileUploadList({ threadId }: { threadId: string }) {
  const [files, setFiles] = useState<UploadedFile[]>([]);

  useEffect(() => {
    fetchFiles();
  }, [threadId]);

  async function fetchFiles() {
    const response = await fetch(`/api/threads/${threadId}/uploads/list`);
    const data = await response.json();
    setFiles(data.files);
  }

  async function handleUpload(event: React.ChangeEvent<HTMLInputElement>) {
    const fileList = event.target.files;
    if (!fileList) return;

    const formData = new FormData();
    Array.from(fileList).forEach(file => {
      formData.append('files', file);
    });

    await fetch(`/api/threads/${threadId}/uploads`, {
      method: 'POST',
      body: formData
    });

    fetchFiles(); // 刷新列表
  }

  async function handleDelete(filename: string) {
    await fetch(`/api/threads/${threadId}/uploads/${filename}`, {
      method: 'DELETE'
    });
    fetchFiles(); // 刷新列表
  }

  return (
    <div>
      <input type="file" multiple onChange={handleUpload} />

      <ul>
        {files.map(file => (
          <li key={file.filename}>
            <span>{file.filename}</span>
            <a href={file.artifact_url} target="_blank">预览</a>
            <a href={`${file.artifact_url}?download=true`}>下载</a>
            {file.markdown_artifact_url && (
              <a href={file.markdown_artifact_url} target="_blank">Markdown</a>
            )}
            <button onClick={() => handleDelete(file.filename)}>删除</button>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

## 注意事项

1. **路径安全性**
   - 实际路径（`path`）包含线程 ID，确保隔离
   - API 会验证路径，防止目录遍历攻击
   - 前端不应直接使用 `path`，而应使用 `artifact_url`

2. **Agent 使用**
   - Agent 只能看到和使用 `virtual_path`
   - 沙箱系统自动映射到实际路径
   - Agent 不需要知道实际的文件系统结构

3. **前端集成**
   - 始终使用 `artifact_url` 访问文件
   - 不要尝试直接访问文件系统路径
   - 使用 `?download=true` 参数强制下载

4. **Markdown 转换**
   - 转换成功时，会返回额外的 `markdown_*` 字段
   - 建议优先使用 Markdown 版本（更易处理）
   - 原始文件始终保留

## 文件**内容**里的路径：本地沙箱怎么翻译

除了路径参数，本地沙箱还会翻译**文件内容里**出现的 `/mnt/user-data/...`。规则按
「这份内容会不会被执行」和「文件在哪」两条轴决定，读写两侧必须对称 —— 不对称时
agent 用 `read_file` 与用 `bash grep` 看到的会是两个互相矛盾的答案，且它无从分辨谁是真的。

| 方向 | 谁 | 行为 |
| --- | --- | --- |
| 写入 | 可执行内容（`.py` / `.sh` / `.js` / `.ts` / 无扩展名…） | `/mnt/...` → 宿主路径。脚本要在宿主上真的能跑 |
| 写入 | 数据文件（`.md` / `.json` / `.csv` / `.txt` / `.yaml`…） | **原样落盘**。内容里的路径是给读者看的值，不是要打开的文件 |
| 读出 | `uploads/` 以外的一切 | 宿主路径 → `/mnt/...` |
| 读出 | `uploads/` 下的文件 | **原样返回**。用户提供的原文是证据，不得改写 |

两条容易踩的推论：

- **判据是「文件在哪」，不是「谁写的」。** 反向翻译不看这个文件是不是经 `write_file` 写的：
  一份由工具（例如 OCR 批处理）写入的产物，读回来同样会被翻译成 `/mnt/...`。
- **需要跨部署可移植的产物必须落 `/mnt/...`。** 数据文件不翻译正是为了这个：写进 `.md`
  的来源标注（provenance）如果变成 `/Users/...`，换一台机器就全部失效。要在数据文件里
  记路径，就用虚拟路径，别用宿主路径。
