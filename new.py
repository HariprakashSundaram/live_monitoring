// ...existing code...
let respTimeChart = new Chart(document.getElementById('respTimeChart'), {
  type: 'line',
  data: { labels: [], datasets: [] },
  options: {
    plugins: {
      legend: { display: false }
    },
    scales: {
      x: { title: { display: true, text: "Time" } },
      y: { title: { display: true, text: "Response Time (ms)" } }
    }
  }
});

async function loadRespTimes(selectedLabels = null) {
  const { start, end } = getRangeParams();
  const testId = getTestId();
  let url = `/api/responsetimes?test_id=${encodeURIComponent(testId)}`;
  if (start) url += `&start=${start}`;
  if (end) url += `&end=${end}`;
  const data = await (await fetch(url)).json();

  // If no label selected, show all
  let labelsToShow = selectedLabels || Object.keys(data);

  // Build datasets
  let datasets = [];
  labelsToShow.forEach(label => {
    if (!data[label]) return;
    datasets.push({
      label: label,
      data: data[label].response_times,
      fill: false,
      borderColor: randomColor(label),
      pointRadius: 1,
      tension: 0.3
    });
  });

  // Use timestamps from the first label for x-axis
  let firstLabel = labelsToShow[0];
  let chartLabels = data[firstLabel] ? data[firstLabel].timestamps.map(ts => moment.unix(ts).format('HH:mm:ss')) : [];

  respTimeChart.data.labels = chartLabels;
  respTimeChart.data.datasets = datasets;
  respTimeChart.update();

  // Build legend
  let legendHtml = `<button class="btn btn-sm btn-outline-secondary me-2" data-label="all">All</button>`;
  Object.keys(data).forEach(label => {
    legendHtml += `<button class="btn btn-sm btn-outline-primary me-2" data-label="${label}">${label}</button>`;
  });
  $('#respTimeLegend').html(legendHtml);

  // Legend click handler
  $('#respTimeLegend button').off('click').on('click', function() {
    let label = $(this).data('label');
    if (label === 'all') {
      loadRespTimes();
    } else {
      loadRespTimes([label]);
    }
  });

  // Range display
  $('#rangeDisplayRespTime').text($('#rangeDisplayAgg').text());
}

// Helper for random color per label
function randomColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
  let color = '#';
  for (let i = 0; i < 3; i++) color += ('00' + ((hash >> (i * 8)) & 0xFF).toString(16)).slice(-2);
  return color;
}

// ...existing code...

async function refreshAll() {
  $('#loadingStatus span').hide();
  updateRangeDisplays();
  await Promise.all([loadTPS(), loadThreads(), loadErrorPct(), loadAggregate(), loadErrors(), loadSuccess(), loadRespTimes()]);
  setAutoRefresh(parseInt($('#refreshSelect').val()));
  $('#loadingStatus span').show();
  setTimeout(() => { $('#loadingStatus span').fadeOut(); }, 2000);
}

// ...existing code...