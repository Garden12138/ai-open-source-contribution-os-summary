use super::*;

#[test]
fn notification_frames_and_task_targets() {
    assert!(parse_frame(b": heartbeat").is_none());
    assert!(parse_frame(b"event: other\ndata: {}").is_none());
    let notice = parse_frame(
        br#"event: notification
data: {"category":"request_waiting","title":"Permission requested","task_id":"task_123"}"#,
    )
    .unwrap();
    assert_eq!(notice.category, "request_waiting");
    assert!(valid_task(notice.task_id.as_deref().unwrap()));
    assert!(!valid_task("a?code=123"));
    assert!(!valid_task("../redirect"));
}
