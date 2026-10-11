import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import mermaid from 'mermaid'
import { describe, expect, it } from 'vitest'

// backend が生成した本物の図（docs/ontology.md）を、本物の mermaid で構文解析する（#85 レビュー）
describe('docs/ontology.md の Mermaid', () => {
  it('mermaid で解析でき、設定（%%{init}%%）を含まない', async () => {
    const md = readFileSync(resolve(__dirname, '../../../docs/ontology.md'), 'utf-8')
    const block = md.split('```mermaid\n')[1]?.split('\n```')[0]
    expect(block).toBeTruthy()
    const result = await mermaid.parse(block!)
    expect(result).toBeTruthy()
    expect(block).not.toMatch(/%%/)
  })
})
