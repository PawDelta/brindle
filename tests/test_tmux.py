from brindle import tmux


def test_control_quote_is_one_argument_tmux_reads_back_as_the_text():
    # tmux's parser undoes \\ \" and \$ and turns \ooo into the byte, so
    # control characters can cross a line-based channel; a NUL can't.
    assert tmux.control_quote('-t %2 "$x\\' + "\n\x7f\x00é") == '"-t %2 \\"\\$x\\\\\\012\\177é"'
