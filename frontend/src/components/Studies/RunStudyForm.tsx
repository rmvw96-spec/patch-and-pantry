import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation } from "@tanstack/react-query"
import { Play } from "lucide-react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { StudiesService, type StudyPublic } from "@/client"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { newIdempotencyKey } from "@/lib/idempotency"
import { handleError } from "@/utils"

const formSchema = z.object({
  prompt: z
    .string()
    .trim()
    .min(1, { message: "Prompt is required" })
    .max(2000, { message: "Prompt must be 2000 characters or fewer" }),
})

type FormData = z.infer<typeof formSchema>

interface RunStudyFormProps {
  onSubmitted: (study: StudyPublic) => void
}

const RunStudyForm = ({ onSubmitted }: RunStudyFormProps) => {
  const { showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onSubmit",
    defaultValues: { prompt: "" },
  })

  const mutation = useMutation({
    // One idempotency key per submission: if the request is retried, the API returns the same
    // study instead of starting a duplicate job.
    mutationFn: async (data: FormData) =>
      (
        await StudiesService.createStudy({
          body: { prompt: data.prompt },
          headers: { "idempotency-key": newIdempotencyKey() },
        })
      ).data,
    onSuccess: (study) => onSubmitted(study),
    onError: handleError.bind(showErrorToast),
  })

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
        className="flex flex-col gap-4 sm:flex-row sm:items-end"
      >
        <FormField
          control={form.control}
          name="prompt"
          render={({ field }) => (
            <FormItem className="flex-1">
              <FormLabel>Study prompt</FormLabel>
              <FormControl>
                <Input
                  placeholder="Optimize case B at 70 bar"
                  type="text"
                  data-testid="study-prompt-input"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <LoadingButton type="submit" loading={mutation.isPending}>
          <Play className="mr-2" />
          Run study
        </LoadingButton>
      </form>
    </Form>
  )
}

export default RunStudyForm
