# EVA4 Reflection Protocol

## Goal

Not patching individual errors, but auditing the methodology itself — finding which core.md
rules are ignored in practice, which are unclear, and which patterns should be formalized.

## Data collection (mandatory — do not skip)

1. **Task statistics** (python_exec):
   ```sql
   SELECT status, COUNT(*) as n FROM tasks GROUP BY status;
   SELECT goal, status, created_at FROM tasks ORDER BY created_at DESC LIMIT 30;
   ```

2. **AAR experiences** (memory_search):
   - `memory_search(query="failure error problem", type="experience", limit=20)`
   - `memory_search(query="SOP workflow method", type="workflow", limit=20)`

3. **Pending upgrade-assessment recommendations**:
   - `memory_search(tags=["upgrade-assessment"], limit=10)`

4. **Memory distribution**:
   ```sql
   SELECT type, COUNT(*), AVG(importance) FROM memories GROUP BY type;
   ```

## Analysis questions

1. Which errors recur? (≥2 same-type failures = rule gap in core.md)
2. Which core.md rules are repeatedly ignored in AAR records?
3. Which successful methods should be formalized as SOP?
4. Which upgrade-assessment recommendations are valid but not yet applied?

## Output rules

- Apply `core_update` only when evidence is sufficient; state the data basis in `reason`
- **Do not** modify any `.py` files or suggest code changes — record findings in memory
- After the session, write a reflection summary to memory:
  `type=experience, tags=["reflect-session"], importance=7`
