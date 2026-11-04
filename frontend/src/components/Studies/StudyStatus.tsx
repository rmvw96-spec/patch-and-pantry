import { useQuery } from "@tanstack/react-query"

import { StudiesService, type StudyPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

type StudyState = StudyPublic["status"]

const FINISHED: StudyState[] = ["succeeded", "failed"]
const POLL_INTERVAL_MS = 1000

const BADGE_VARIANT: Record<
  StudyState,
  "default" | "secondary" | "destructive"
> = {
  queued: "secondary",
  running: "secondary",
  succeeded: "default",
  failed: "destructive",
}

function isFinished(study: StudyPublic | undefined): boolean {
  return study !== undefined && FINISHED.includes(study.status)
}

const StudyStatus = ({ studyId }: { studyId: string }) => {
  const { data: study, isError } = useQuery({
    queryKey: ["study", studyId],
    queryFn: async () =>
      (await StudiesService.readStudy({ path: { id: studyId } })).data,
    // Poll while the job is queued or running; stop as soon as it reaches a final state.
    refetchInterval: (query) =>
      isFinished(query.state.data) ? false : POLL_INTERVAL_MS,
  })

  if (isError) {
    return (
      <p className="text-destructive" data-testid="study-load-error">
        Could not load this study. Refresh the page to try again.
      </p>
    )
  }

  if (!study) {
    return null
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3">
          Study
          {/* aria-live announces status changes to screen readers as polling updates them */}
          <span aria-live="polite">
            <Badge
              variant={BADGE_VARIANT[study.status]}
              data-testid="study-status"
            >
              {study.status}
            </Badge>
          </span>
        </CardTitle>
        <CardDescription data-testid="study-prompt">
          {study.prompt}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!isFinished(study) && (
          <p className="text-muted-foreground">
            Running. This updates automatically.
          </p>
        )}

        {study.status === "succeeded" && study.result && (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2">
            <dt className="text-muted-foreground">
              Objective (outlet pressure)
            </dt>
            <dd data-testid="study-objective">{study.result.objective} bar</dd>
            <dt className="text-muted-foreground">Feasible</dt>
            <dd data-testid="study-feasible">
              {study.result.feasible ? "Yes" : "No"}
            </dd>
            {study.result.review && (
              <>
                <dt className="text-muted-foreground">Review</dt>
                <dd data-testid="study-review">{study.result.review}</dd>
              </>
            )}
          </dl>
        )}

        {study.status === "failed" && (
          <p className="text-destructive" data-testid="study-error">
            {study.error_message ?? "The study failed."}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

export default StudyStatus
