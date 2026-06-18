---
name: Refactor
description: 重构手册，提取方法、消除重复、SOLID 原则应用指南
triggers:
  - refactor
  - 重构
  - clean up
  - simplify
  - extract
---

## Refactoring Principles

### When to Refactor
- Before adding a feature (make the change easy, then make the easy change)
- After getting tests to pass (red → green → **refactor**)
- When you notice duplication (Rule of Three: tolerate twice, refactor on third)
- When a function exceeds ~30 lines or does more than one thing

### When NOT to Refactor
- No tests exist for the code (write tests first)
- Under time pressure with no safety net
- Working code that nobody will touch again
- Refactoring for aesthetics without functional benefit

## Common Refactoring Patterns

### Extract Method
**When**: A block of code does a distinct subtask
```python
# Before
def process():
    # ... 20 lines of validation ...
    # ... 30 lines of transformation ...
    # ... 10 lines of saving ...

# After
def process():
    validated = self._validate(data)
    transformed = self._transform(validated)
    self._save(transformed)
```

### Replace Conditional with Polymorphism
**When**: switch/if-elif chains that dispatch on type
```python
# Before
if shape.type == "circle": area = pi * r**2
elif shape.type == "rect": area = w * h

# After
class Circle: def area(self): return pi * self.r**2
class Rect: def area(self): return self.w * self.h
```

### Introduce Parameter Object
**When**: Multiple functions pass the same group of parameters
```python
# Before: def search(query, page, limit, sort_by, order)
# After:  def search(params: SearchParams)
```

### Replace Magic Numbers/Strings with Constants
```python
# Before: if retries > 3:
# After:  MAX_RETRIES = 3; if retries > MAX_RETRIES:
```

### Simplify Boolean Logic
```python
# Before: if not (x > 0 and not y):
# After:  if x <= 0 or y:
```

## SOLID Quick Reference

| Principle | One-liner | Smell when violated |
|-----------|-----------|-------------------|
| **S**ingle Responsibility | One reason to change | Class does file I/O AND business logic |
| **O**pen/Closed | Extend, don't modify | Adding a type requires editing 5 switch statements |
| **L**iskov Substitution | Subtypes are interchangeable | Subclass throws NotImplementedError |
| **I**nterface Segregation | Small, focused interfaces | Client depends on methods it never calls |
| **D**ependency Inversion | Depend on abstractions | High-level module imports low-level detail |

## Refactoring Process

1. **Ensure tests exist** — you need a safety net
2. **Make one change at a time** — commit after each refactoring step
3. **Run tests after each change** — catch regressions immediately
4. **Preserve behavior** — refactoring means NO functional changes
5. **Name things better** — good names eliminate the need for comments

## Code Smells → Refactoring

| Smell | Refactoring |
|-------|-------------|
| Long method (>30 lines) | Extract Method |
| Duplicated code | Extract Method / Pull Up |
| Long parameter list (>3) | Introduce Parameter Object |
| Feature envy (method uses another class's data) | Move Method |
| Data clumps (same fields always together) | Extract Class |
| Primitive obsession (string for email, int for ID) | Value Object |

### For: $ARGUMENTS
Apply these refactoring techniques to the code described above.