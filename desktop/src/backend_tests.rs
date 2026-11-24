use super::*;

#[test]
fn only_the_expected_ready_line_is_accepted() {
    assert!(ready_line(b"CONTRIBOS_READY port=8765", 8765));
    assert!(ready_line(b"CONTRIBOS_READY port=8765\r\n", 8765));
    for line in [
        &b"CONTRIBOS_READY port=876"[..],
        b"CONTRIBOS_READY port=8765 extra",
        b"Log: CONTRIBOS_READY port=8765",
        b"CONTRIBOS_READY port=99999",
        b"CONTRIBOS_READY port=\xff",
    ] {
        assert!(!ready_line(line, 8765));
    }
}
