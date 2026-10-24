package net.nekomami.cubej1;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;

/**
 * The background half of the sync.
 *
 * A periodic job rather than a resident service: a foreground service would
 * mean a permanent notification for work that is a single HTTP request every
 * so often, and the platform would fight a background service anyway. The
 * unmetered constraint keeps it off mobile data, where it would only ever
 * fail.
 */
public class SyncJob extends JobService {

    private static final int JOB_ID = 4701;
    /** 15 minutes is the platform's floor for periodic work. */
    private static final long INTERVAL_MS = 15 * 60 * 1000L;

    static void schedule(Context c) {
        JobScheduler js = (JobScheduler) c.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (js == null) {
            return;
        }
        JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(c, SyncJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
                .setPeriodic(INTERVAL_MS)
                // Survives a reboot, so there is no boot receiver to keep in
                // step. Needs RECEIVE_BOOT_COMPLETED in the manifest.
                .setPersisted(true)
                .build();
        js.schedule(job);
    }

    static boolean scheduled(Context c) {
        JobScheduler js = (JobScheduler) c.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        return js != null && js.getPendingJob(JOB_ID) != null;
    }

    @Override
    public boolean onStartJob(final JobParameters params) {
        final Context ctx = getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                Sync.run(ctx);
                // Never ask for a reschedule: the next period comes round on
                // its own, and a failure here usually just means "not home".
                jobFinished(params, false);
            }
        }, "cubej1-sync").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }
}
