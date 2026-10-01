from django.contrib.postgres.search import SearchHeadline, SearchQuery
from django.db import connection
from django.db.models import Q
from django.shortcuts import render

from ..models import Application, Opportunity, Person, Task
from .documents import visible_documents


def _search(user, q, limit):
    apps = Application.objects.select_related("funder").filter(
        Q(title__icontains=q) | Q(short_name__icontains=q) | Q(abstract__icontains=q) | Q(notes__icontains=q)
        | Q(sponsor_id__icontains=q) | Q(internal_id__icontains=q) | Q(mechanism__iexact=q) | Q(major_goals__icontains=q)
        | Q(lessons_learned__icontains=q) | Q(award__award_number__icontains=q)
    ).distinct()[:limit]
    opps = Opportunity.objects.select_related("funder").filter(
        Q(title__icontains=q) | Q(number__icontains=q) | Q(summary__icontains=q) | Q(mechanism__iexact=q)
    )[:limit]
    docs = visible_documents(user).select_related("application").filter(
        Q(title__icontains=q) | Q(filename__icontains=q) | Q(extracted_text__icontains=q) | Q(notes__icontains=q)
    )
    if connection.vendor == "postgresql":
        docs = docs.annotate(
            snippet=SearchHeadline(
                "extracted_text", SearchQuery(q, search_type="websearch"),
                start_sel="\x02", stop_sel="\x03", max_words=30, min_words=12, max_fragments=1,
            )
        )
    docs = docs.defer("extracted_text")[:limit]
    people = Person.objects.filter(
        Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q) | Q(institution__icontains=q)
    )[:limit]
    tasks = Task.objects.select_related("application").filter(title__icontains=q)[:limit]
    return {"applications": apps, "opportunities": opps, "documents": docs, "people": people, "tasks": tasks}


def quick_search(request):
    q = request.GET.get("q", "").strip()
    results = _search(request.user, q, 5) if len(q) >= 2 else None
    return render(request, "grants/search/_quick.html", {"q": q, "results": results})


def search(request):
    q = request.GET.get("q", "").strip()
    results = _search(request.user, q, 50) if len(q) >= 2 else None
    return render(request, "grants/search/results.html", {"q": q, "results": results})
