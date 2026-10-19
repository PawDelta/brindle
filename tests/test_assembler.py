from brindle.assembler import DECISIONS, DOCUMENT_CHUNKS, OTHER_SESSIONS, TASK_AND_RULES, Block, assemble


def test_assembler_drops_lowest_priority_block():
    task, decisions, sessions, docs = "t" * 40, "x" * 40, "s" * 40, "d" * 80
    blocks = [Block(DOCUMENT_CHUNKS, docs), Block(TASK_AND_RULES, task),
              Block(OTHER_SESSIONS, sessions), Block(DECISIONS, decisions)]
    assert assemble(blocks) == "\n\n".join((docs, task, sessions, decisions))   # no cap: all kept
    # 10 + 10 + 10 tokens fit in 30; the 20-token document block does not.
    assert assemble(blocks, 30) == "\n\n".join((task, sessions, decisions))
    # Under 20 the other-sessions block goes too; the survivors stay whole.
    assert assemble(blocks, 25) == "\n\n".join((task, decisions))
    # The top tier is never dropped, even when it alone is over the cap.
    assert assemble(blocks, 1) == task
