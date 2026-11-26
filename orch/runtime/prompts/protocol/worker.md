Start anything that takes more than a few minutes as a background job and end your turn; you are woken when it ends, so
don't wait in a loop. Say what runs and where its log is.
{job_line}
Never use nohup, setsid, a trailing & or another shell trick to keep something running: such a job gets lost.
