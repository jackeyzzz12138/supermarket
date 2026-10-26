import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'

test('arXiv search validates queries, parses Atom and respects cache/rate limits', () => {
  const result = spawnSync('python3', ['-B', import.meta.dirname + '/arxiv_search_test.py'], {
    encoding: 'utf8',
    timeout: 15_000,
  })
  expect(result.error).toBeUndefined()
  expect(result.stderr).not.toContain('FAILED')
  expect(result.status, result.stdout + result.stderr).toBe(0)
})
