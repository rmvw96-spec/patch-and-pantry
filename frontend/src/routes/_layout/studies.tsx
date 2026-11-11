import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { z } from "zod"

import RunStudyForm from "@/components/Studies/RunStudyForm"
import StudyStatus from "@/components/Studies/StudyStatus"

// The current study lives in the URL (?id=...), so a refresh or a shared link shows the same study.
const searchSchema = z.object({
  id: z.string().optional().catch(undefined),
})

export const Route = createFileRoute("/_layout/studies")({
  component: Studies,
  validateSearch: searchSchema,
  head: () => ({
    meta: [
      {
        title: "Studies - Patch & Pantry",
      },
    ],
  }),
})

function Studies() {
  const { id } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Studies</h1>
        <p className="text-muted-foreground">
          Describe a pipeline case and the agent runs the simulation for you
        </p>
      </div>
      <RunStudyForm
        onSubmitted={(study) => navigate({ search: { id: study.id } })}
      />
      {id && <StudyStatus key={id} studyId={id} />}
    </div>
  )
}
