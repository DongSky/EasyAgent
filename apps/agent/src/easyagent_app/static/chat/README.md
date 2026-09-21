# Chat page code

Start at `../workspace-chat.js`: it connects the composer, conversation selection,
refresh lifecycle, result cards and event handlers.

- `requests.js`: outgoing message shape and cached progress reads.
- `state.js`: derived display state from the backend's phase definitions.
- `views.js`: page and card markup, connection setup and status headings.
- `graph.js`: graph layout and incremental DOM updates, preserving focus and animation.
- `activity.js`: incremental event cursor and activity rendering.

These modules use the existing `api`, escaping and error helpers supplied by Studio.
The controller checks the selected conversation after asynchronous reads. Keep those
checks when moving code: a late response must not paint a different conversation.

To format browser source from the repository root:

```sh
npx --yes prettier@3.6.2 --write "apps/agent/src/easyagent_app/static/**/*.js"
```

No frontend build step or JavaScript dependency is needed to run the application.
