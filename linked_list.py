"""
A comprehensive singly linked list implementation in Python.

Supports:
- Basic operations: append, prepend, insert, delete, search
- Iteration and indexing
- Reversal, sorting (merge sort), deduplication
- Conversion to/from Python lists
"""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Generator,
    Generic,
    Iterable,
    Iterator,
    Optional,
    TypeVar,
)

T = TypeVar("T")


class ListNode(Generic[T]):
    """A single node in a singly linked list."""

    __slots__ = ("value", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.next: Optional[ListNode[T]] = None

    def __repr__(self) -> str:
        return f"ListNode({self.value!r})"


class LinkedList(Generic[T]):
    """Singly linked list with head pointer and optional tail pointer."""

    def __init__(self, iterable: Optional[Iterable[T]] = None) -> None:
        self._head: Optional[ListNode[T]] = None
        self._tail: Optional[ListNode[T]] = None
        self._size: int = 0

        if iterable is not None:
            for val in iterable:
                self.append(val)

    # ------------------------------------------------------------------
    #  Size / emptiness
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return self._size

    def is_empty(self) -> bool:
        return self._size == 0

    # ------------------------------------------------------------------
    #  Head / tail access
    # ------------------------------------------------------------------

    @property
    def head(self) -> Optional[ListNode[T]]:
        return self._head

    @property
    def tail(self) -> Optional[ListNode[T]]:
        return self._tail

    # ------------------------------------------------------------------
    #  Insertion
    # ------------------------------------------------------------------

    def prepend(self, value: T) -> None:
        """Insert value at the front of the list."""
        node = ListNode(value)
        node.next = self._head
        self._head = node
        if self._tail is None:
            self._tail = node
        self._size += 1

    def append(self, value: T) -> None:
        """Insert value at the end of the list."""
        node = ListNode(value)
        if self._tail is None:
            self._head = self._tail = node
        else:
            self._tail.next = node
            self._tail = node
        self._size += 1

    def insert(self, index: int, value: T) -> None:
        """
        Insert value at the given index (0-based).

        Raises IndexError if index < 0 or index > len(list).
        """
        if index < 0 or index > self._size:
            raise IndexError(f"LinkedList index out of range: {index}")

        if index == 0:
            self.prepend(value)
            return

        if index == self._size:
            self.append(value)
            return

        prev = self._node_at(index - 1)
        node = ListNode(value)
        node.next = prev.next
        prev.next = node
        self._size += 1

    def extend(self, other: Iterable[T]) -> None:
        """Append every element from *other*."""
        for val in other:
            self.append(val)

    # ------------------------------------------------------------------
    #  Deletion
    # ------------------------------------------------------------------

    def pop(self, index: int = -1) -> T:
        """
        Remove and return the element at *index* (default last).

        Raises IndexError on empty list or bad index.
        """
        if self._head is None:
            raise IndexError("pop from empty list")
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(f"LinkedList index out of range: {index}")

        if index == 0:
            value = self._head.value
            self._head = self._head.next
            if self._head is None:
                self._tail = None
            self._size -= 1
            return value

        prev = self._node_at(index - 1)
        target = prev.next
        assert target is not None
        value = target.value
        prev.next = target.next
        if target is self._tail:
            self._tail = prev
        self._size -= 1
        return value

    def remove(self, value: T) -> None:
        """
        Remove the first occurrence of *value*.

        Raises ValueError if the value is not present.
        """
        prev: Optional[ListNode[T]] = None
        curr = self._head
        while curr is not None:
            if curr.value == value:
                if prev is None:
                    self._head = curr.next
                else:
                    prev.next = curr.next
                if curr is self._tail:
                    self._tail = prev
                self._size -= 1
                return
            prev = curr
            curr = curr.next
        raise ValueError(f"LinkedList.remove({value!r}): value not found")

    def clear(self) -> None:
        """Remove all elements."""
        self._head = None
        self._tail = None
        self._size = 0

    # ------------------------------------------------------------------
    #  Access / search
    # ------------------------------------------------------------------

    def _node_at(self, index: int) -> ListNode[T]:
        """Return node at 0-based index (caller validates bounds)."""
        curr = self._head
        for _ in range(index):
            assert curr is not None
            curr = curr.next
        assert curr is not None
        return curr

    def __getitem__(self, index: int) -> T:
        if not isinstance(index, int):
            raise TypeError(f"list indices must be integers, not {type(index)}")
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(f"LinkedList index out of range: {index}")
        return self._node_at(index).value

    def __setitem__(self, index: int, value: T) -> None:
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(f"LinkedList index out of range: {index}")
        self._node_at(index).value = value

    def index(self, value: T, start: int = 0, stop: Optional[int] = None) -> int:
        """Return the first index of *value*. Raises ValueError."""
        stop = stop if stop is not None else self._size
        curr = self._head
        i = 0
        while curr is not None and i < stop:
            if i >= start and curr.value == value:
                return i
            curr = curr.next
            i += 1
        raise ValueError(f"{value!r} is not in list")

    def __contains__(self, value: Any) -> bool:
        curr = self._head
        while curr is not None:
            if curr.value == value:
                return True
            curr = curr.next
        return False

    # ------------------------------------------------------------------
    #  Iteration
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[T]:
        curr = self._head
        while curr is not None:
            yield curr.value
            curr = curr.next

    def nodes(self) -> Generator[ListNode[T], None, None]:
        """Yield ListNode objects (useful for debugging)."""
        curr = self._head
        while curr is not None:
            yield curr
            curr = curr.next

    # ------------------------------------------------------------------
    #  Slicing
    # ------------------------------------------------------------------

    def __getitem_slice(self, s: slice) -> LinkedList[T]:
        return LinkedList(list(self)[s])

    def __setitem_slice(self, s: slice, values: Iterable[T]) -> None:
        vals = list(values)
        as_list = list(self)
        as_list[s] = vals
        self.clear()
        self.extend(as_list)

    # ------------------------------------------------------------------
    #  Reversal
    # ------------------------------------------------------------------

    def reverse(self) -> None:
        """Reverse the list in-place."""
        prev: Optional[ListNode[T]] = None
        curr = self._head
        self._tail = curr
        while curr is not None:
            nxt = curr.next
            curr.next = prev
            prev = curr
            curr = nxt
        self._head = prev

    # ------------------------------------------------------------------
    #  Sorting (merge sort – O(n log n), stable)
    # ------------------------------------------------------------------

    def sort(self, key: Optional[Callable[[T], Any]] = None, reverse: bool = False) -> None:
        """Sort the list in-place using iterative merge sort."""
        if self._size < 2:
            return

        # Break into sorted runs and repeatedly merge
        key_fn = key if key is not None else (lambda x: x)

        def _merge(
            left: Optional[ListNode[T]],
            right: Optional[ListNode[T]],
        ) -> tuple[Optional[ListNode[T]], Optional[ListNode[T]]]:
            """Merge two sorted lists, return (new_head, new_tail)."""
            dummy = ListNode(None)  # type: ignore[arg-type]
            tail: ListNode[T] = dummy
            while left is not None and right is not None:
                if key_fn(left.value) <= key_fn(right.value):
                    tail.next = left
                    left = left.next
                else:
                    tail.next = right
                    right = right.next
                tail = tail.next
            tail.next = left if left is not None else right
            # Walk to new tail
            while tail.next is not None:
                tail = tail.next
            return dummy.next, tail

        # Iterative merge sort: split list into sub-lists of size 1,2,4,...
        head = self._head
        step = 1
        while step < self._size:
            left: Optional[ListNode[T]] = head
            head = None
            tail: Optional[ListNode[T]] = None
            while left is not None:
                # Split into two halves of size `step`
                right = left
                for _ in range(step):
                    if right is None:
                        break
                    right = right.next
                # Detach the first half
                second = right
                first = left
                # Detach second half start
                right_start = second
                for _ in range(step):
                    if right_start is None:
                        break
                    right_start = right_start.next
                # Detach first half end
                left_end = left
                for _ in range(step - 1):
                    if left_end is None:
                        break
                    left_end = left_end.next

                if left_end is not None:
                    left_end.next = None

                # Detach second half end
                if second is not None:
                    right_end = second
                    for _ in range(step - 1):
                        if right_end is None or right_end.next is None:
                            break
                        right_end = right_end.next
                    if right_end is not None:
                        # Save next chunk start before detaching
                        nxt = right_end.next
                        right_end.next = None
                    else:
                        nxt = None
                else:
                    nxt = None

                merged_head, merged_tail = _merge(first, second)
                if head is None:
                    head = merged_head
                if tail is not None:
                    tail.next = merged_head
                tail = merged_tail

                left = nxt

            self._head = head
            self._tail = tail
            step <<= 1

        if reverse:
            self.reverse()

    # ------------------------------------------------------------------
    #  Copy
    # ------------------------------------------------------------------

    def copy(self) -> LinkedList[T]:
        """Return a shallow copy."""
        return LinkedList(self)

    __copy__ = copy

    # ------------------------------------------------------------------
    #  Conversion
    # ------------------------------------------------------------------

    def to_list(self) -> list[T]:
        return list(self)

    @classmethod
    def from_list(cls, items: list[T]) -> LinkedList[T]:
        return cls(items)

    # ------------------------------------------------------------------
    #  String representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        items = [repr(v) for v in self]
        return f"LinkedList([{', '.join(items)}])"

    def __str__(self) -> str:
        return " -> ".join(str(v) for v in self)

    # ------------------------------------------------------------------
    #  Equality
    # ------------------------------------------------------------------

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, LinkedList):
            return NotImplemented
        if len(self) != len(other):
            return False
        return all(a == b for a, b in zip(self, other))
