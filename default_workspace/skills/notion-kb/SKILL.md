---
name: notion-kb
description: Conventions for Egor's Notion workspace - the Knowledge, Tasks and Projects databases, their fields, and the search-before-answer, write-after workflow.
when_to_use:
  - Saving a note, link, reference or decision to Notion
  - Searching Notion for something the user wrote down or decided earlier
  - Creating or editing a page in the Knowledge, Tasks or Projects databases
---

# Notion knowledge base

Data sources (pass the id to `notion-query-data-sources`, or as `data_source_id` in a create-pages `parent`):

- Knowledge: `collection://41821f6c-e439-813d-9672-000bc69cfa1c`
- Tasks: `collection://0b2c30e0-3aa2-47bb-85b7-69a1032cca73`
- Projects: `collection://63d67809-155a-4319-ad71-a0686800abef`

## Workflow

- Search before answering: `notion-search` / query Knowledge first, then answer from what you found and link the pages.
- Write conclusions after: when a question is settled, save the conclusion only if the user asked for it to be saved.
- Fetch a page before editing it. Append with `insert_content`; do not rewrite existing text.
- Report the link of every page created or changed.

## Knowledge pages

- Name: short, specific title.
- Summary: 1-3 sentences.
- Type, Area, Tags: set when clear from the request; do not invent values.
- Project: link one Project if the user named it.
- Status: never treat a page with Status `Outdated` as fact; say it is outdated.
- Source: `Telegram`.
- Created by agent: checked.

## Tasks (read and context only)

- Statuses: Inbox, Next, Doing, Waiting, Done, Dropped. Priority P1-P4.
- Area is always set once a task is triaged; a task has one Project.
- Inbox means not triaged yet. Never change a task's Status or set it to Dropped; adding tasks and status changes (complete, drop) go through `task-assistant`. You may set Area, Project or Scheduled on an existing task with `update_properties` when Pickle or `task-assistant` routes that to you.
